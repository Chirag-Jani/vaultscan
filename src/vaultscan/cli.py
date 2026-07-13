"""CLI entrypoint: vaultscan fetch-idl / fetch-all / parse-idl / parse-all."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from vaultscan.config import load_config
from vaultscan.idl_fetch import IdlFetchError, FetchResult, fetch_all, fetch_idl
from vaultscan.idl_parse import ParseResult, load_and_parse, parse_all_idls, resolve_idl_path

STATUS_ORDER = ("false", "ambiguous", "true")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vaultscan",
        description="VaultScan — Solana vault/PDA lockup detection",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    fetch_one = sub.add_parser("fetch-idl", help="Fetch one program IDL via Anchor CLI")
    fetch_one.add_argument("program_id", help="On-chain program address")

    sub.add_parser("fetch-all", help="Fetch IDLs for all entries in data/programs.json")

    parse_one = sub.add_parser(
        "parse-idl",
        help="Stage 1: parse one IDL for close-path signals",
    )
    parse_one.add_argument(
        "target",
        help="Path to IDL JSON, program_id under data/idls/, or fixture filename",
    )
    parse_one.add_argument(
        "-o",
        "--out",
        help="Optional output JSON path (default: data/parsed/<name>.json)",
    )
    parse_one.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print instruction evidence per account",
    )

    parse_all = sub.add_parser(
        "parse-all",
        help="Stage 1: parse all IDLs in data/idls/",
    )
    parse_all.add_argument(
        "--also-fixtures",
        action="store_true",
        help="Also parse data/fixtures/*.json",
    )
    parse_all.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print instruction evidence per account",
    )
    return parser


def _default_parse_out(data_dir: Path, source: Path) -> Path:
    out_dir = data_dir / "parsed"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{source.stem}.json"


def _write_parse_result(result: ParseResult, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(result.to_dict(), f, indent=2)
        f.write("\n")


def _short_error(message: str) -> str:
    """Keep first meaningful line of Anchor/CLI stderr."""
    for line in message.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("caused by"):
            continue
        # Drop our own remediation trailer for display; keep root cause.
        if line.startswith("Ensure the program"):
            continue
        if "anchor idl fetch failed" in line and ": " in line:
            line = line.split(": ", 1)[1]
        return line[:160]
    return message.strip()[:160] or "unknown error"


def _ix_ref(names: list[str]) -> str:
    if not names:
        return "-"
    if len(names) == 1:
        return names[0]
    return f"{names[0]} (+{len(names) - 1})"


def _program_label(result: ParseResult) -> str:
    name = result.program_name or "unknown"
    if result.program_id:
        return f"{name}  {result.program_id}"
    return name


def _print_parse_summary(result: ParseResult, *, verbose: bool = False) -> None:
    accounts = result.account_types
    counts = {s: 0 for s in STATUS_ORDER}
    for a in accounts:
        counts[a.has_close_path] = counts.get(a.has_close_path, 0) + 1

    print(_program_label(result))
    print(
        f"  close_path  false={counts['false']}  "
        f"ambiguous={counts['ambiguous']}  true={counts['true']}"
    )

    if not accounts:
        print("  (no candidates)")
        return

    # Group by status: candidates first (false), then ambiguous, then true
    by_status: dict[str, list] = {s: [] for s in STATUS_ORDER}
    for a in accounts:
        by_status.setdefault(a.has_close_path, []).append(a)

    for status in STATUS_ORDER:
        group = by_status.get(status) or []
        if not group:
            continue
        print(f"  [{status}]")
        for a in group:
            if verbose:
                print(
                    f"    {a.account_type:<28} "
                    f"create={_ix_ref(a.creating_instructions):<28} "
                    f"close={_ix_ref(a.close_instructions):<20} "
                    f"reclaim={_ix_ref(a.reclaim_instructions)}"
                )
            else:
                print(f"    {a.account_type}")


def _print_fetch_result(result: FetchResult) -> None:
    if result.ok:
        print(f"ok    {result.program_id}")
    else:
        print(f"fail  {result.program_id}  {_short_error(result.error or '')}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    config = load_config()

    if args.command == "fetch-idl":
        try:
            result = fetch_idl(config, args.program_id)
        except IdlFetchError as exc:
            print(f"fail  {args.program_id}  {_short_error(str(exc))}", file=sys.stderr)
            return 1
        _print_fetch_result(result)
        return 0

    if args.command == "fetch-all":
        results = fetch_all(config)
        ok_count = sum(1 for r in results if r.ok)
        for r in results:
            _print_fetch_result(r)
        print(f"fetched {ok_count}/{len(results)}")
        return 0 if ok_count == len(results) else 1

    if args.command == "parse-idl":
        try:
            path = resolve_idl_path(config.data_dir, args.target)
        except FileNotFoundError as exc:
            print(f"error  {exc}", file=sys.stderr)
            return 1
        result = load_and_parse(path)
        out = Path(args.out) if args.out else _default_parse_out(config.data_dir, path)
        _write_parse_result(result, out)
        _print_parse_summary(result, verbose=args.verbose)
        print(f"wrote  {out}")
        return 0

    if args.command == "parse-all":
        results = parse_all_idls(config.idls_dir)
        if args.also_fixtures:
            for path in sorted(config.fixtures_dir.glob("*.json")):
                results.append(load_and_parse(path))
        if not results:
            print("error  no IDLs found under data/idls/", file=sys.stderr)
            return 1
        out_dir = config.data_dir / "parsed"
        out_dir.mkdir(parents=True, exist_ok=True)
        totals = {s: 0 for s in STATUS_ORDER}
        for result in results:
            stem = Path(result.source_path).stem
            out = out_dir / f"{stem}.json"
            _write_parse_result(result, out)
            _print_parse_summary(result, verbose=args.verbose)
            print(f"wrote  {out}")
            print()
            for a in result.account_types:
                totals[a.has_close_path] = totals.get(a.has_close_path, 0) + 1
        print(
            f"parsed {len(results)}  "
            f"false={totals['false']}  ambiguous={totals['ambiguous']}  true={totals['true']}"
        )
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
