"""CLI for rec-serve."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        print(
            f"error: environment variable {name}={raw!r} is not a valid integer",
            file=sys.stderr,
        )
        raise SystemExit(2)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        print(
            f"error: environment variable {name}={raw!r} is not a valid float",
            file=sys.stderr,
        )
        raise SystemExit(2)


def _default_rec_bin() -> str:
    return os.environ.get("REC_BIN") or shutil.which("rec") or os.path.expanduser(
        "~/.local/bin/rec"
    )


def cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rec-serve",
        description="Always-on rec (Recursive Flint) API (rec serve + exposing proxy)",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("REC_SERVE_HOST", "127.0.0.1"),
        help="Public bind host (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=_env_int("REC_SERVE_PORT", 11501),
        help="Public bind port (default: 11501)",
    )
    parser.add_argument(
        "--backend-port",
        type=int,
        default=_env_int("REC_SERVE_BACKEND_PORT", 11500),
        help="Internal rec serve port on 127.0.0.1 (default: 11500, rec's default)",
    )
    parser.add_argument(
        "--rec-bin",
        default=_default_rec_bin(),
        help="Path to rec binary (default: rec on PATH)",
    )
    parser.add_argument(
        "--context",
        type=int,
        default=_env_int("REC_SERVE_CONTEXT", 0) or None,
        help="Pass --context TOKENS to rec serve (default: set by the release)",
    )
    parser.add_argument(
        "--load-timeout",
        type=float,
        default=_env_float("REC_SERVE_BACKEND_TIMEOUT", 300.0),
        help="Seconds to wait for rec serve to become healthy (default: 300)",
    )
    parser.add_argument(
        "--keepalive-timeout",
        type=float,
        default=_env_float("REC_SERVE_KEEPALIVE_TIMEOUT", 3600.0),
        help="Idle seconds before releasing sleep prevention (default: 3600)",
    )
    parser.add_argument(
        "--no-keepalive",
        action="store_true",
        help="Disable macOS sleep prevention",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify rec availability and exit",
    )

    args = parser.parse_args(argv)

    if args.check:
        if not shutil.which(args.rec_bin):
            print(f"rec not found: {args.rec_bin}")
            return 1
        res = subprocess.run(
            [args.rec_bin, "--version"], capture_output=True, text=True
        )
        print(f"rec ok: {args.rec_bin} ({res.stdout.strip() or res.stderr.strip()})")
        return 0

    from rec_serve.proxy import run_server

    run_server(
        host=args.host,
        port=args.port,
        backend_port=args.backend_port,
        rec_bin=args.rec_bin,
        context=args.context,
        load_timeout=args.load_timeout,
        keepalive_timeout=args.keepalive_timeout,
        enable_keepalive=not args.no_keepalive,
    )
    return 0
