"""`pio project config` for PIO firmware, run in the background (design doc §12.5).

The query is read-only and takes about two seconds, so the tracker never waits for it:
it asks for the cached result, which is refreshed in a worker thread whenever
platformio.ini changed. While no result is known the configured-target check is skipped.
"""

import os
import shutil
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from embr.core.firmware import import_pio
from embr.core.firmware.import_pio import (
    PLATFORMIO_INI,
    EnvConfig,
    EnvRef,
    PioUnavailableError,
    Runner,
    run_pio,
)

PIO_SECTION = "pio"
FRAMEWORK_PKG_KEY = "framework_pkg"
"""settings.ini [pio] framework_pkg: the global manual framework package path."""


@dataclass(frozen=True)
class ProjectConfig:
    envs: Mapping[str, EnvConfig]
    """By env name; empty when the query failed."""
    error: str | None = None
    """Why the query failed (pio not found, failed, timed out); None on success."""


_Stamp = tuple[int, int]


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
        self._start = start or _start_thread
        self._on_ready = on_ready
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[_Stamp, ProjectConfig]] = {}
        self._running: set[str] = set()

    @property
    def core_dir(self) -> Path:
        """import_pio.core_dir() of the environment."""
        return import_pio.core_dir(self._environ)

    def cached(self, project_root: Path) -> ProjectConfig | None:
        """The result for platformio.ini as it is now (size and modification time). When
        there is none, or the file changed since, one background query is started (never
        two at once for the same root) and None is returned. A failure is cached like a
        result until the file changes; a missing platformio.ini gives a ProjectConfig with
        an error, without running pio."""
        stamp = _stamp(project_root)
        if stamp is None:
            return ProjectConfig({}, f"{project_root / PLATFORMIO_INI} not found")
        key = _key(project_root)
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and hit[0] == stamp:
                return hit[1]
            if key in self._running:
                return None
            self._running.add(key)

        def work() -> None:
            try:
                result = self._query(project_root)
                with self._lock:
                    self._cache[key] = (stamp, result)
            finally:
                with self._lock:
                    self._running.discard(key)
            if self._on_ready is not None:
                self._on_ready(project_root)

        self._start(work)
        return None

    def load(self, project_root: Path) -> ProjectConfig:
        """Query now and wait (the import dialog runs this off the UI thread); the result
        is cached as cached() would. Never raises for pio problems (see error)."""
        stamp = _stamp(project_root)
        if stamp is None:
            return ProjectConfig({}, f"{project_root / PLATFORMIO_INI} not found")
        result = self._query(project_root)
        with self._lock:
            self._cache[_key(project_root)] = (stamp, result)
        return result

    def target(self, ref: EnvRef) -> str | None:
        """import_pio.configured_target() of the env from cached(); None while unknown."""
        config = self.env(ref)
        if config is None:
            return None
        return import_pio.configured_target(config, ref.project_root, self.core_dir)

    def env(self, ref: EnvRef) -> EnvConfig | None:
        """The env's EnvConfig from cached(); None while unknown."""
        found = self.cached(ref.project_root)
        return found.envs.get(ref.env) if found is not None else None

    def _query(self, project_root: Path) -> ProjectConfig:
        pio = import_pio.find_pio(self._environ, self._which)
        if pio is None:
            return ProjectConfig({}, "pio was not found (install PlatformIO or add it to PATH)")
        try:
            return ProjectConfig(import_pio.query_config(pio, project_root, self._run))
        except PioUnavailableError as exc:
            return ProjectConfig({}, str(exc))


def _start_thread(work: Callable[[], None]) -> None:
    threading.Thread(target=work, name="pio-config", daemon=True).start()


def _stamp(project_root: Path) -> _Stamp | None:
    try:
        st = (project_root / PLATFORMIO_INI).stat()
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


def _key(project_root: Path) -> str:
    return os.path.normcase(str(project_root.resolve()))
