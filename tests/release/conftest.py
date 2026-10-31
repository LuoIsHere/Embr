"""Release suite gate and shared fixtures (see release_kit.py for the harness).

Every test under tests/release is marked `release` and skipped unless EMBR_RELEASE=1.
Run the suite as its own invocation, so no other conftest (tests/ui forces the offscreen
platform) is loaded:

    EMBR_RELEASE=1 uv run pytest tests/release

Fixtures:

* qapp: the QApplication on the real Windows platform (fails on offscreen);
* embr: factory start(root=None, language=None) -> release_kit.EmbrApp, the real window
  on a fresh portable folder (tmp_path/"embr" by default); every window is closed after
  the test;
* file_dialogs: release_kit.FileDialogs installed for the test; its queue must be used up;
* sentinels: release_kit.Sentinels under tmp_path, for child processes;
* idf_project / pio_project (session): builds_kit.IdfProject / PioProject, the real test
  projects of EMBR_TEST_IDF_PROJECT / EMBR_TEST_PIO_PROJECT, backed up before their first
  use and, after the session (also on failure), restored byte for byte, rebuilt for their
  original target and verified (a problem fails the session's teardown).

For every test: exceptions that reach sys.excepthook (raised in Qt slots / timers) fail
it, and a failed test saves screenshots of every window and the screen in its tmp_path
(the path is printed in the report).
"""

import os
from collections.abc import Callable, Generator, Iterator
from pathlib import Path
from typing import Any

import builds_kit
import pytest
import release_kit as kit

HERE = Path(__file__).resolve().parent

if kit.release_enabled() and os.environ.get("QT_QPA_PLATFORM", "").lower() == "offscreen":
    del os.environ["QT_QPA_PLATFORM"]  # before any Qt GUI object exists


def _ours(item: pytest.Item) -> bool:
    return HERE in Path(str(item.fspath)).resolve().parents


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    skip = pytest.mark.skip(reason=f"release suite: set {kit.RELEASE_VAR}=1 to run")
    others = [i for i in items if not _ours(i)]
    for item in items:
        if not _ours(item):
            continue
        item.add_marker(pytest.mark.release)
        if not kit.release_enabled():
            item.add_marker(skip)
        elif others:
            item.add_marker(
                pytest.mark.skip(
                    reason="release suite must run as its own invocation: "
                    "EMBR_RELEASE=1 uv run pytest tests/release"
                )
            )


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    if report.when == "call":
        item.stash[_FAILED] = report.failed
    return report


_FAILED = pytest.StashKey[bool]()


@pytest.fixture(autouse=True)
def _release_guard(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[None]:
    errors = kit.SlotErrors()
    errors.install()
    try:
        yield
    finally:
        errors.uninstall()
        if request.node.stash.get(_FAILED, False):
            saved = kit.screenshot_all(tmp_path / "screenshots", "failure")
            if saved:
                print(f"\nscreenshots: {saved[0].parent}")
    if errors.errors:
        pytest.fail(f"uncaught exceptions in Qt callbacks: {errors.errors!r}")


@pytest.fixture
def qapp() -> Any:
    return kit.qt_app()


@pytest.fixture
def file_dialogs() -> Iterator[kit.FileDialogs]:
    dialogs = kit.FileDialogs()
    dialogs.install()
    try:
        yield dialogs
    finally:
        dialogs.uninstall()
    dialogs.assert_consumed()


@pytest.fixture
def embr(tmp_path: Path, qapp: Any) -> Iterator[Callable[..., kit.EmbrApp]]:
    started: list[kit.EmbrApp] = []

    def start(root: Path | None = None, language: Any = None) -> kit.EmbrApp:
        app = kit.start_embr(root or tmp_path / "embr", language)
        started.append(app)
        return app

    try:
        yield start
    finally:
        for app in reversed(started):
            app.close()


@pytest.fixture
def sentinels(tmp_path: Path) -> kit.Sentinels:
    return kit.Sentinels(tmp_path / "sentinels")


@pytest.fixture(scope="session")
def idf_project(tmp_path_factory: pytest.TempPathFactory) -> Iterator[builds_kit.IdfProject]:
    root = kit.env_path(builds_kit.IDF_PROJECT_VAR)
    idf = builds_kit.idf_main()
    original = builds_kit.build_facts(root / "build")
    if original is None:
        pytest.fail(f"{root} has no build: build the project once before the release suite")
    store = tmp_path_factory.mktemp("idf_project_backup")
    backup = builds_kit.ProjectBackup(root, store, frozenset({"build", ".git"}))
    backup.capture()
    project = builds_kit.IdfProject(root, idf, backup, builds_kit.git_status(root), original)
    print(f"[idf_project] {root} backed up to {store}")
    try:
        yield project
    finally:
        problems = project.restore_final()
        if problems:
            raise AssertionError(f"{root} not restored (backup in {store}): {problems}")


@pytest.fixture(scope="session")
def pio_project(tmp_path_factory: pytest.TempPathFactory) -> Iterator[builds_kit.PioProject]:
    root = kit.env_path(builds_kit.PIO_PROJECT_VAR)
    builds_kit.find_pio()
    envs = builds_kit.ini_envs_of(root)
    if not envs:
        pytest.fail(f"{root / builds_kit.PIO_INI} declares no env")
    store = tmp_path_factory.mktemp("pio_project_backup")
    backup = builds_kit.ProjectBackup(root, store, frozenset({".pio"}))
    backup.capture()
    project = builds_kit.PioProject(root, backup, envs[0])
    print(f"[pio_project] {root} backed up to {store}")
    try:
        yield project
    finally:
        problems = project.restore_final()
        if problems:
            raise AssertionError(f"{root} not restored (backup in {store}): {problems}")
