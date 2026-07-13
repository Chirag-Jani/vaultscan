"""Stage 1 — static Anchor IDL parse for vault/PDA close-path signals.

Modern Anchor IDLs usually omit compile-time constraints (`init`, `close`) from
JSON. This stage therefore combines:

1. Legacy constraint fields when present (`init` / `initIfNeeded` / `close`)
2. PDA + instruction-name heuristics (create/init/open/make vs close/refund)
3. Reclaim/admin/sweep name heuristics → ``ambiguous`` (Arthur FP concern)

Output per account type: ``has_close_path`` in {true, false, ambiguous}.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

# Explicit close / account-lifetime end
CLOSE_NAME_RE = re.compile(
    r"(^close($|_))|(^refund($|_))|(^cancel($|_))",
    re.IGNORECASE,
)

# Creation / init of PDAs or vault-like accounts
CREATE_NAME_RE = re.compile(
    r"(^init($|_))|(^initialize)|(^(create|open|make)($|_))",
    re.IGNORECASE,
)

# Generic reclaim — not named close, but may move value out
RECLAIM_NAME_RE = re.compile(
    r"(withdraw|sweep|reclaim|emergency|drain|rescue|admin_|"
    r"collect_fund|collect_protocol|collect_creator|collect_remaining|"
    r"claim($|_))",
    re.IGNORECASE,
)


@dataclass
class AccountTypeFinding:
    account_type: str
    creating_instructions: list[str] = field(default_factory=list)
    close_instructions: list[str] = field(default_factory=list)
    reclaim_instructions: list[str] = field(default_factory=list)
    has_close_path: str = "false"  # true | false | ambiguous
    evidence: list[str] = field(default_factory=list)


@dataclass
class ParseResult:
    program_id: str | None
    program_name: str | None
    source_path: str
    account_types: list[AccountTypeFinding]
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "program_id": self.program_id,
            "program_name": self.program_name,
            "source_path": self.source_path,
            "notes": self.notes,
            "account_types": [asdict(a) for a in self.account_types],
        }


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _program_meta(idl: dict[str, Any]) -> tuple[str | None, str | None]:
    program_id = idl.get("address")
    if isinstance(program_id, str) and program_id:
        pid: str | None = program_id
    else:
        pid = None
    meta = idl.get("metadata")
    name = None
    if isinstance(meta, dict) and isinstance(meta.get("name"), str):
        name = meta["name"]
    elif isinstance(idl.get("name"), str):
        name = idl["name"]
    return pid, name


def _legacy_constraint_flags(account: dict[str, Any]) -> tuple[bool, bool, bool]:
    """Return (is_init, is_init_if_needed, has_close) from legacy IDL shapes."""
    is_init = bool(account.get("init") or account.get("isInit"))
    is_init_if_needed = bool(
        account.get("initIfNeeded")
        or account.get("init_if_needed")
        or account.get("isInitIfNeeded")
    )
    has_close = False
    if account.get("close") is not None:
        has_close = True
    # Nested constraints array (older / alternate emits)
    constraints = account.get("constraints")
    if isinstance(constraints, list):
        for c in constraints:
            if not isinstance(c, dict):
                continue
            kind = str(c.get("name") or c.get("kind") or "").lower()
            if kind in {"init", "initialize"}:
                is_init = True
            if kind in {"initifneeded", "init_if_needed"}:
                is_init_if_needed = True
            if kind == "close":
                has_close = True
    return is_init, is_init_if_needed, has_close


def _defined_account_types(idl: dict[str, Any]) -> dict[str, str]:
    """Map normalized name → display name for IDL account structs."""
    out: dict[str, str] = {}
    for entry in idl.get("accounts") or []:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            out[_norm(entry["name"])] = entry["name"]
    # Types that look like account structs (fallback when accounts[] is thin)
    for entry in idl.get("types") or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            continue
        t = entry.get("type")
        if isinstance(t, dict) and t.get("kind") == "struct":
            key = _norm(entry["name"])
            out.setdefault(key, entry["name"])
    return out


def _resolve_type(
    account_name: str, defined: dict[str, str]
) -> str | None:
    """Map instruction account field name to an IDL account type when possible."""
    key = _norm(account_name)
    if key in defined:
        return defined[key]
    # e.g. pool_state → PoolState, escrow → Escrow
    for def_key, display in defined.items():
        if key == def_key or key.endswith(def_key) or def_key.endswith(key):
            return display
    return None


def _iter_ix_accounts(ix: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for acc in ix.get("accounts") or []:
        if isinstance(acc, dict) and isinstance(acc.get("name"), str):
            yield acc


def parse_idl(idl: dict[str, Any], *, source_path: str = "") -> ParseResult:
    program_id, program_name = _program_meta(idl)
    defined = _defined_account_types(idl)
    notes: list[str] = []

    # Per-type accumulators
    creating: dict[str, set[str]] = {}
    closing: dict[str, set[str]] = {}
    reclaiming: dict[str, set[str]] = {}
    evidence: dict[str, list[str]] = {}
    seen_types: set[str] = set(defined.values())

    def touch(atype: str) -> None:
        seen_types.add(atype)
        creating.setdefault(atype, set())
        closing.setdefault(atype, set())
        reclaiming.setdefault(atype, set())
        evidence.setdefault(atype, [])

    # Seed defined account types so they appear even if unused in ixs
    for display in defined.values():
        touch(display)

    instructions = idl.get("instructions") or []
    if not isinstance(instructions, list):
        notes.append("IDL instructions field missing or not a list")
        instructions = []

    legacy_constraint_hits = 0

    for ix in instructions:
        if not isinstance(ix, dict):
            continue
        ix_name = ix.get("name")
        if not isinstance(ix_name, str):
            continue

        is_create_ix = bool(CREATE_NAME_RE.search(ix_name))
        is_close_ix = bool(CLOSE_NAME_RE.search(ix_name))
        is_reclaim_ix = bool(RECLAIM_NAME_RE.search(ix_name)) and not is_close_ix

        for acc in _iter_ix_accounts(ix):
            acc_name = acc["name"]
            is_init, is_init_if_needed, has_close_constraint = _legacy_constraint_flags(acc)
            if is_init or is_init_if_needed or has_close_constraint:
                legacy_constraint_hits += 1

            resolved = _resolve_type(acc_name, defined)
            has_pda = bool(acc.get("pda"))
            writable = bool(acc.get("writable") or acc.get("isMut"))

            # Candidate account type identity:
            # prefer resolved IDL account type; else PDA field name as synthetic type
            if resolved:
                atype = resolved
            elif has_pda or is_init or is_init_if_needed:
                atype = acc_name
            else:
                continue

            touch(atype)

            if is_init or is_init_if_needed:
                creating[atype].add(ix_name)
                kind = "init_if_needed" if is_init_if_needed else "init"
                evidence[atype].append(
                    f"legacy constraint {kind} on '{acc_name}' in ix '{ix_name}'"
                )

            if has_close_constraint:
                closing[atype].add(ix_name)
                evidence[atype].append(
                    f"legacy close constraint on '{acc_name}' in ix '{ix_name}'"
                )

            if is_create_ix and (has_pda or writable):
                creating[atype].add(ix_name)
                evidence[atype].append(
                    f"create-like ix '{ix_name}' writes account '{acc_name}'"
                    + (" (pda)" if has_pda else "")
                )

            if is_close_ix and writable:
                closing[atype].add(ix_name)
                evidence[atype].append(
                    f"close-like ix '{ix_name}' writes account '{acc_name}'"
                )

            if is_reclaim_ix and writable:
                reclaiming[atype].add(ix_name)
                evidence[atype].append(
                    f"reclaim-like ix '{ix_name}' writes account '{acc_name}'"
                )

    if legacy_constraint_hits == 0:
        notes.append(
            "No legacy init/close constraints in IDL; used PDA + instruction-name heuristics"
        )

    # Only report types that look like vault/PDA candidates:
    # created somehow, or only had close/reclaim with a defined type + evidence of PDA lifecycle
    findings: list[AccountTypeFinding] = []
    for atype in sorted(seen_types, key=str.lower):
        creates = sorted(creating.get(atype, set()))
        closes = sorted(closing.get(atype, set()))
        reclaim = sorted(reclaiming.get(atype, set()))
        ev = evidence.get(atype, [])

        # Skip inert type defs with zero lifecycle signal
        if not creates and not closes and not reclaim:
            continue

        if closes:
            status = "true"
        elif reclaim:
            status = "ambiguous"
        elif creates:
            status = "false"
        else:
            status = "ambiguous"

        # Dedupe evidence while preserving order
        seen_ev: set[str] = set()
        uniq_ev: list[str] = []
        for item in ev:
            if item not in seen_ev:
                seen_ev.add(item)
                uniq_ev.append(item)

        findings.append(
            AccountTypeFinding(
                account_type=atype,
                creating_instructions=creates,
                close_instructions=closes,
                reclaim_instructions=reclaim,
                has_close_path=status,
                evidence=uniq_ev,
            )
        )

    return ParseResult(
        program_id=program_id if isinstance(program_id, str) else None,
        program_name=program_name if isinstance(program_name, str) else None,
        source_path=source_path,
        account_types=findings,
        notes=notes,
    )


def load_and_parse(path: Path) -> ParseResult:
    with path.open(encoding="utf-8") as f:
        idl = json.load(f)
    if not isinstance(idl, dict):
        raise ValueError(f"IDL root must be object: {path}")
    return parse_idl(idl, source_path=str(path))


def resolve_idl_path(data_dir: Path, program_id_or_path: str) -> Path:
    """Accept a filesystem path or a program_id under data/idls/."""
    direct = Path(program_id_or_path).expanduser()
    if direct.is_file():
        return direct
    candidate = data_dir / "idls" / f"{program_id_or_path}.json"
    if candidate.is_file():
        return candidate
    fixture = data_dir / "fixtures" / program_id_or_path
    if fixture.is_file():
        return fixture
    raise FileNotFoundError(
        f"IDL not found: {program_id_or_path} "
        f"(tried path, {candidate}, fixtures/{program_id_or_path})"
    )


def parse_all_idls(idls_dir: Path) -> list[ParseResult]:
    results: list[ParseResult] = []
    for path in sorted(idls_dir.glob("*.json")):
        if path.name == "index.json":
            continue
        results.append(load_and_parse(path))
    return results
