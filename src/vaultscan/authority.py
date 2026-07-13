"""Stage 2 — on-chain upgrade-authority check via Solana CLI."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vaultscan.config import Config
from vaultscan.idl_fetch import load_programs


class AuthorityError(RuntimeError):
    """Raised when authority cannot be determined."""


@dataclass
class AuthorityResult:
    program_id: str
    ok: bool
    authority_status: str  # renounced | live | unknown
    authority: str | None
    owner: str | None
    programdata_address: str | None
    checked_at: str
    source: str | None
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _require_solana_cli() -> str:
    path = shutil.which("solana")
    if not path:
        raise AuthorityError(
            "Solana CLI not found on PATH. Install solana and retry."
        )
    return path


def _classify_authority(raw: Any) -> tuple[str, str | None]:
    """Map solana program show authority field → (status, pubkey|None)."""
    if raw is None:
        return "renounced", None
    if isinstance(raw, str):
        value = raw.strip()
        if not value or value.lower() in {"none", "null"}:
            return "renounced", None
        return "live", value
    return "unknown", None


def check_authority(config: Config, program_id: str) -> AuthorityResult:
    """Query upgrade authority for one program. Fail loudly on CLI/RPC errors."""
    checked_at = _utc_now()
    program_id = program_id.strip()
    if not program_id:
        raise AuthorityError("program_id must be non-empty")

    try:
        solana = _require_solana_cli()
    except AuthorityError as exc:
        result = AuthorityResult(
            program_id=program_id,
            ok=False,
            authority_status="unknown",
            authority=None,
            owner=None,
            programdata_address=None,
            checked_at=checked_at,
            source=None,
            error=str(exc),
        )
        _persist(config, result)
        raise

    cmd = [
        solana,
        "program",
        "show",
        program_id,
        "-u",
        config.rpc_url,
        "--output",
        "json",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=os.environ.copy(),
            check=False,
        )
    except OSError as exc:
        result = AuthorityResult(
            program_id=program_id,
            ok=False,
            authority_status="unknown",
            authority=None,
            owner=None,
            programdata_address=None,
            checked_at=checked_at,
            source="solana-cli",
            error=f"failed to run Solana CLI: {exc}",
        )
        _persist(config, result)
        raise AuthorityError(result.error) from exc

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        result = AuthorityResult(
            program_id=program_id,
            ok=False,
            authority_status="unknown",
            authority=None,
            owner=None,
            programdata_address=None,
            checked_at=checked_at,
            source="solana-cli",
            error=f"solana program show failed: {detail}",
        )
        _persist(config, result)
        raise AuthorityError(result.error)

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        result = AuthorityResult(
            program_id=program_id,
            ok=False,
            authority_status="unknown",
            authority=None,
            owner=None,
            programdata_address=None,
            checked_at=checked_at,
            source="solana-cli",
            error=f"non-JSON output from solana program show: {exc}",
        )
        _persist(config, result)
        raise AuthorityError(result.error) from exc

    if not isinstance(payload, dict):
        result = AuthorityResult(
            program_id=program_id,
            ok=False,
            authority_status="unknown",
            authority=None,
            owner=None,
            programdata_address=None,
            checked_at=checked_at,
            source="solana-cli",
            error="solana program show JSON root must be an object",
        )
        _persist(config, result)
        raise AuthorityError(result.error)

    status, authority = _classify_authority(payload.get("authority"))
    if status == "unknown":
        result = AuthorityResult(
            program_id=program_id,
            ok=False,
            authority_status="unknown",
            authority=None,
            owner=payload.get("owner") if isinstance(payload.get("owner"), str) else None,
            programdata_address=(
                payload.get("programdataAddress")
                if isinstance(payload.get("programdataAddress"), str)
                else None
            ),
            checked_at=checked_at,
            source="solana-cli",
            error=f"unrecognized authority field: {payload.get('authority')!r}",
        )
        _persist(config, result)
        raise AuthorityError(result.error)

    result = AuthorityResult(
        program_id=program_id,
        ok=True,
        authority_status=status,
        authority=authority,
        owner=payload.get("owner") if isinstance(payload.get("owner"), str) else None,
        programdata_address=(
            payload.get("programdataAddress")
            if isinstance(payload.get("programdataAddress"), str)
            else None
        ),
        checked_at=checked_at,
        source="solana-cli",
        error=None,
    )
    _persist(config, result)
    return result


def check_all(config: Config) -> list[AuthorityResult]:
    """Check authority for every program in data/programs.json."""
    programs = load_programs(config)
    results: list[AuthorityResult] = []
    for entry in programs:
        program_id = entry.get("program_id")
        if not program_id or not isinstance(program_id, str):
            results.append(
                AuthorityResult(
                    program_id=str(program_id),
                    ok=False,
                    authority_status="unknown",
                    authority=None,
                    owner=None,
                    programdata_address=None,
                    checked_at=_utc_now(),
                    source=None,
                    error="missing or invalid program_id in programs.json",
                )
            )
            continue
        try:
            results.append(check_authority(config, program_id))
        except AuthorityError as exc:
            results.append(
                AuthorityResult(
                    program_id=program_id,
                    ok=False,
                    authority_status="unknown",
                    authority=None,
                    owner=None,
                    programdata_address=None,
                    checked_at=_utc_now(),
                    source="solana-cli",
                    error=str(exc),
                )
            )
    return results


def _authority_dir(config: Config) -> Path:
    path = config.data_dir / "authority"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _persist(config: Config, result: AuthorityResult) -> None:
    out_dir = _authority_dir(config)
    out_path = out_dir / f"{result.program_id}.json"
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(result.to_dict(), f, indent=2)
        f.write("\n")

    index_path = out_dir / "index.json"
    if index_path.is_file():
        with index_path.open(encoding="utf-8") as f:
            index = json.load(f)
        if not isinstance(index, dict):
            index = {"entries": {}}
    else:
        index = {"entries": {}}
    index.setdefault("entries", {})
    index["entries"][result.program_id] = result.to_dict()
    with index_path.open("w", encoding="utf-8") as f:
        json.dump(index, f, indent=2)
        f.write("\n")
