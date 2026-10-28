"""Command-line entry point."""

import argparse
import sys

from embr import __version__


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["--worker"]:
        from embr.core.flasher.worker import run

        return run(sys.stdin, sys.stdout)
    parser = argparse.ArgumentParser(prog="Embr")
    parser.add_argument("--version", action="version", version=f"Embr {__version__}")
    parser.parse_args(args)
    return 0
