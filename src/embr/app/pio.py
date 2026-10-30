"""`pio project config` for PIO firmware, run in the background (design doc §12.5).

The query is read-only and takes about two seconds, so the tracker never waits for it:
it asks for the cached result, which is refreshed in a worker thread whenever
platformio.ini changed. While no result is known the configured-target check is skipped.
"""

import os
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from embr.core.firmware.import_pio import EnvConfig, EnvRef, Runner, run_pio


@dataclass(frozen=True)
class ProjectConfig:
    envs: Mapping[str, EnvConfig]
    """By env name; empty when the query failed."""
    error: str | None = None
    """Why the query failed (pio not found, failed, timed out); None on success."""


class PioConfigs:
    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        run: Runner = run_pio,
        which: Callable[[str], str | None] = shutil.which,
        start: Callable[[Callable[[], None]], None] | None = None,
        on_ready: Callable[[Path], None] | None = None,
    ) -> None:
        """`environ` defaults to os.environ. `start` runs a callable in the background
        (default: a daemon thread). `on_ready(project_root)` is called from that thread
        after each background query, success or failure."""
        self._environ = environ if environ is not None else os.environ
        self._run = run
        self._which = which
        self._start = start
        self._on_ready = on_ready

    @property
    def core_dir(self) -> Path:
        """import_pio.core_dir() of the environment."""
        raise NotImplementedError

    def cached(self, project_root: Path) -> ProjectConfig | None:
        """The result for platformio.ini as it is now (size and modification time). When
        there is none, or the file changed since, one background query is started (never
        two at once for the same root) and None is returned. A failure is cached like a
        result until the file changes; a missing platformio.ini gives a ProjectConfig with
        an error, without running pio."""
        raise NotImplementedError

    def load(self, project_root: Path) -> ProjectConfig:
        """Query now and wait (the import dialog runs this off the UI thread); the result
        is cached as cached() would. Never raises for pio problems (see error)."""
        raise NotImplementedError

    def target(self, ref: EnvRef) -> str | None:
        """import_pio.configured_target() of the env from cached(); None while unknown."""
        raise NotImplementedError

    def env(self, ref: EnvRef) -> EnvConfig | None:
        """The env's EnvConfig from cached(); None while unknown."""
        raise NotImplementedError
