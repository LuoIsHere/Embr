"""Command-line entry point."""

import argparse

from embr import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="Embr")
    parser.add_argument("--version", action="version", version=f"Embr {__version__}")
    parser.parse_args(argv)
    return 0
