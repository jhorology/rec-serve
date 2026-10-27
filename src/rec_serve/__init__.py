"""rec-serve: always-on Recursive rec (Flint) server exposed via a proxy."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("rec-serve")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0"


def main() -> None:
    import sys

    from rec_serve.cli import cli_main

    sys.exit(cli_main())
