"""CLI entrypoint: vaultscan fetch-idl / fetch-all."""

from __future__ import annotations

import argparse
import sys

from vaultscan.config import load_config
from vaultscan.idl_fetch import IdlFetchError, fetch_all, fetch_idl


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vaultscan",
        description="VaultScan — Solana vault/PDA lockup detection",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    fetch_one = sub.add_parser("fetch-idl", help="Fetch one program IDL via Anchor CLI")
    fetch_one.add_argument("program_id", help="On-chain program address")

    sub.add_parser("fetch-all", help="Fetch IDLs for all entries in data/programs.json")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    config = load_config()

    if args.command == "fetch-idl":
        try:
            result = fetch_idl(config, args.program_id)
        except IdlFetchError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"ok: wrote {result.path} (source={result.source})")
        return 0

    if args.command == "fetch-all":
        results = fetch_all(config)
        ok_count = sum(1 for r in results if r.ok)
        for r in results:
            if r.ok:
                print(f"ok:   {r.program_id} -> {r.path}")
            else:
                print(f"fail: {r.program_id}: {r.error}", file=sys.stderr)
        print(f"done: {ok_count}/{len(results)} succeeded")
        return 0 if ok_count == len(results) else 1

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
