"""Fetch Anchor IDLs via the Anchor CLI and persist them under data/idls/."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vaultscan.config import Config


class IdlFetchError(RuntimeError):
    """Raised when an IDL cannot be fetched."""


@dataclass
class FetchResult:
    program_id: str
    ok: bool
    path: Path | None
    source: str | None
    error: str | None
    fetched_at: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_programs(config: Config) -> list[dict[str, Any]]:
    path = config.programs_path
    if not path.is_file():
        raise FileNotFoundError(f"programs list not found: {path}")
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"expected a JSON array in {path}")
    return data


def _require_anchor_cli() -> str:
    path = shutil.which("anchor")
    if not path:
        raise IdlFetchError(
            "Anchor CLI not found on PATH. Install Anchor "
            "(https://www.anchor-lang.com/docs/installation) and retry."
        )
    return path


def _validate_idl(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise IdlFetchError(f"anchor idl fetch returned non-JSON output: {exc}") from exc
    if not isinstance(payload, dict):
        raise IdlFetchError("IDL root must be a JSON object")
    # Anchor IDLs expose either legacy `name` or newer `metadata.name` / `address`.
    has_name = "name" in payload or (
        isinstance(payload.get("metadata"), dict) and "name" in payload["metadata"]
    )
    if not has_name and "instructions" not in payload:
        raise IdlFetchError("IDL missing expected Anchor fields (name/instructions)")
    return payload


def _load_index(config: Config) -> dict[str, Any]:
    path = config.idl_index_path
    if not path.is_file():
        return {"entries": {}}
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        return {"entries": {}}
    data.setdefault("entries", {})
    return data


def _save_index(config: Config, index: dict[str, Any]) -> None:
    config.idls_dir.mkdir(parents=True, exist_ok=True)
    with config.idl_index_path.open("w", encoding="utf-8") as f:
        json.dump(index, f, indent=2)
        f.write("\n")


def _update_index(config: Config, result: FetchResult) -> None:
    index = _load_index(config)
    entry: dict[str, Any] = {
        "ok": result.ok,
        "fetched_at": result.fetched_at,
        "source": result.source,
        "path": str(result.path) if result.path else None,
        "error": result.error,
    }
    index["entries"][result.program_id] = entry
    _save_index(config, index)


def fetch_idl(config: Config, program_id: str) -> FetchResult:
    """Fetch one IDL with Anchor CLI; fail loudly on missing CLI or fetch error."""
    fetched_at = _utc_now()
    program_id = program_id.strip()
    if not program_id:
        raise IdlFetchError("program_id must be non-empty")

    try:
        anchor = _require_anchor_cli()
    except IdlFetchError as exc:
        result = FetchResult(
            program_id=program_id,
            ok=False,
            path=None,
            source=None,
            error=str(exc),
            fetched_at=fetched_at,
        )
        _update_index(config, result)
        raise

    config.idls_dir.mkdir(parents=True, exist_ok=True)
    out_path = config.idls_dir / f"{program_id}.json"

    cmd = [
        anchor,
        "idl",
        "fetch",
        program_id,
        "--provider.cluster",
        config.cluster,
        "-o",
        str(out_path),
    ]
    env = {**os.environ, "ANCHOR_PROVIDER_URL": config.rpc_url}

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
    except OSError as exc:
        result = FetchResult(
            program_id=program_id,
            ok=False,
            path=None,
            source="anchor-cli",
            error=f"failed to run Anchor CLI: {exc}",
            fetched_at=fetched_at,
        )
        _update_index(config, result)
        raise IdlFetchError(result.error) from exc

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        if out_path.is_file():
            out_path.unlink(missing_ok=True)
        result = FetchResult(
            program_id=program_id,
            ok=False,
            path=None,
            source="anchor-cli",
            error=(
                f"anchor idl fetch failed for {program_id}: {detail}. "
                "Ensure the program publishes an IDL on-chain, "
                f"RPC ({config.rpc_url}) is reachable, and Anchor CLI is installed."
            ),
            fetched_at=fetched_at,
        )
        _update_index(config, result)
        raise IdlFetchError(result.error)

    raw = out_path.read_text(encoding="utf-8")
    try:
        payload = _validate_idl(raw)
    except IdlFetchError as exc:
        out_path.unlink(missing_ok=True)
        result = FetchResult(
            program_id=program_id,
            ok=False,
            path=None,
            source="anchor-cli",
            error=str(exc),
            fetched_at=fetched_at,
        )
        _update_index(config, result)
        raise

    # Rewrite pretty-printed for readability
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")

    result = FetchResult(
        program_id=program_id,
        ok=True,
        path=out_path,
        source="anchor-cli",
        error=None,
        fetched_at=fetched_at,
    )
    _update_index(config, result)
    return result


def fetch_all(config: Config) -> list[FetchResult]:
    """Fetch IDLs for every program in data/programs.json. Continues on per-program errors."""
    programs = load_programs(config)
    results: list[FetchResult] = []
    for entry in programs:
        program_id = entry.get("program_id")
        if not program_id or not isinstance(program_id, str):
            results.append(
                FetchResult(
                    program_id=str(program_id),
                    ok=False,
                    path=None,
                    source=None,
                    error="missing or invalid program_id in programs.json",
                    fetched_at=_utc_now(),
                )
            )
            continue
        try:
            results.append(fetch_idl(config, program_id))
        except IdlFetchError as exc:
            results.append(
                FetchResult(
                    program_id=program_id,
                    ok=False,
                    path=None,
                    source="anchor-cli",
                    error=str(exc),
                    fetched_at=_utc_now(),
                )
            )
    return results
