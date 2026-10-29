"""Command-line entry point."""

import argparse
import io
import os
import sys
from importlib.resources import files

from embr import __version__


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["--worker"]:
        # esptool.loader reads esptool.cfg once, when it is first imported, and the worker
        # package imports esptool; so the bundled config must be set before that import
        os.environ["ESPTOOL_CFGFILE"] = esptool_config_path()
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


def esptool_config_path() -> str:
    """Bundled esptool.cfg, so user-level esptool configs cannot change behaviour."""
    return str(files("embr.resources").joinpath("esptool.cfg"))
