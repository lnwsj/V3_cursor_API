"""Tiny CLI helper for managing the local GreenStats identity.

Usage:
    py -m core.identity_cli                       # show current + bootstrap if missing
    py -m core.identity_cli bootstrap             # force-create temp nickname
    py -m core.identity_cli rename <new-value>   # rename current user_id
    py -m core.identity_cli path                 # print resolved config path

Designed so that running the desktop app for the very first time shows up
in the public leaderboard as soon as the first event is uploaded.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional, Sequence

from core.usage_stats import (
    bootstrap_temp_identity,
    load_config,
    normalize_identity,
    save_identity_config,
)


def _print(identity) -> None:
    print(f"identity_type:  {identity.identity_type}")
    print(f"identity_value: {identity.identity_value}")
    cfg_path = Path.home() / ".green_pc" / "greenstats.json"
    print(f"config_path:    {cfg_path}")


def main(argv: Sequence[str]) -> int:
    if not argv:
        cfg = load_config()
        if cfg.identity and cfg.identity.identity_value:
            print("current identity (from disk):")
            _print(cfg.identity)
        else:
            print("no identity set — bootstrapping temp nickname...")
            identity = bootstrap_temp_identity()
            print("created:")
            _print(identity)
        return 0

    cmd = argv[0]
    if cmd == "bootstrap":
        identity = bootstrap_temp_identity()
        print("bootstrapped:")
        _print(identity)
        return 0

    if cmd == "path":
        from core.usage_stats import _config_path
        print(_config_path())
        return 0

    if cmd == "rename":
        if len(argv) < 2:
            print("usage: identity_cli rename <new-value>")
            print("       identity_cli rename email <addr>     # set to email type")
            print("       identity_cli rename phone +66xxx    # set to phone type")
            print("       identity_cli rename user_id name01   # set to user_id type")
            return 2
        new = argv[1].strip()
        kind = "user_id"
        if len(argv) >= 3:
            kind = argv[1].strip().lower()
            new = argv[2].strip()
        try:
            norm = normalize_identity(kind, new)
        except (TypeError, ValueError) as exc:
            print(f"rename failed: {exc}")
            return 2
        if norm is None:
            print("rename failed: invalid value")
            return 2
        identity = save_identity_config(
            identity_type=norm.identity_type,
            identity_value=norm.identity_value,
        )
        print("renamed:")
        _print(identity)
        return 0

    print(f"unknown subcommand: {cmd}")
    print("usage: identity_cli [bootstrap | rename <value> | path]")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
