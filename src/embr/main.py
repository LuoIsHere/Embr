"""Command-line entry point."""

import argparse
import io
import sys

from embr import __version__


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["--worker"]:
        from embr.core.flasher.worker import run

        # pipes default to block buffering and the ANSI code page; events need neither
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            if isinstance(stream, io.TextIOWrapper):
                stream.reconfigure(encoding="utf-8", line_buffering=True)
        return run(sys.stdin, sys.stdout)
    parser = argparse.ArgumentParser(prog="Embr")
    parser.add_argument("--version", action="version", version=f"Embr {__version__}")
    parser.parse_args(args)
    return 0
