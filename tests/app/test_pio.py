"""app/pio: `pio project config` results cached per project and refreshed in the background
when platformio.ini changes (design doc §12.5). pio is never run: a FakePio answers from
the platformio.ini it is run in; `start` collects the background jobs so tests run them."""

import os
from collections.abc import Callable
from pathlib import Path

import pytest
from app_testkit import FakePio, pio_configs, pio_project, write_ini

from embr.app.pio import PioConfigs
from embr.core.firmware.import_pio import EnvRef

BOARDS = {"esp32dev": "esp32dev", "esp32-s3-devkitc-1": "esp32-s3-devkitc-1"}


class Jobs(list[Callable[[], None]]):
    """`start` for PioConfigs that keeps the jobs instead of running them."""

    def run(self, index: int = -1) -> None:
        self[index]()


def _project(tmp_path: Path) -> tuple[Path, Path]:
    root, core = tmp_path / "project", tmp_path / "core"
    pio_project("multi_env", root, core)
    return root, core


def test_cached_starts_one_background_query(tmp_path: Path) -> None:
    root, core = _project(tmp_path)
    runner, jobs, ready = FakePio(), Jobs(), list[Path]()
    configs = pio_configs(core, runner, start=jobs.append, on_ready=ready.append)

    assert configs.cached(root) is None and configs.cached(root) is None  # never two at once
    assert len(jobs) == 1 and runner.calls == []
    jobs.run()
    assert ready == [root]
    result = configs.cached(root)
    assert result is not None and result.error is None
    assert {name: env.board for name, env in result.envs.items()} == BOARDS
    assert result.envs["esp32dev"].frameworks == ("arduino",)  # inherited from [env]
    assert configs.cached(root) == result
    assert (len(jobs), len(runner.calls)) == (1, 1)


@pytest.mark.parametrize("change", ["content", "mtime"])
def test_a_changed_platformio_ini_is_queried_again(tmp_path: Path, change: str) -> None:
    root, core = _project(tmp_path)
    runner, jobs = FakePio(), Jobs()
    configs = pio_configs(core, runner, start=jobs.append)
    configs.cached(root)
    jobs.run()
    assert configs.cached(root) is not None

    ini = root / "platformio.ini"
    if change == "content":
        write_ini(root, ini.read_text().replace("board = esp32dev", "board = esp32-s3-devkitc-1"))
    else:
        mtime = ini.stat().st_mtime_ns + 10**9
        os.utime(ini, ns=(mtime, mtime))
    assert configs.cached(root) is None and len(jobs) == 2
    jobs.run()
    again = configs.cached(root)
    assert again is not None and again.error is None
    expected = "esp32-s3-devkitc-1" if change == "content" else "esp32dev"
    assert again.envs["esp32dev"].board == expected
    assert len(runner.calls) == 2


def test_a_failure_is_cached_until_platformio_ini_changes(tmp_path: Path) -> None:
    root, core = _project(tmp_path)
    runner, jobs, ready = FakePio(), Jobs(), list[Path]()
    configs = pio_configs(core, runner, start=jobs.append, on_ready=ready.append)
    runner.fail = True
    assert configs.cached(root) is None
    jobs.run()
    assert ready == [root]
    failed = configs.cached(root)
    assert failed is not None and failed.error and not failed.envs
    assert configs.cached(root) == failed and len(jobs) == 1

    runner.fail = False
    write_ini(root, (root / "platformio.ini").read_text() + "\n")
    assert configs.cached(root) is None
    jobs.run()
    recovered = configs.cached(root)
    assert recovered is not None and recovered.error is None and recovered.envs


def test_missing_platformio_ini_is_an_error_without_running_pio(tmp_path: Path) -> None:
    _, core = _project(tmp_path)
    runner, jobs = FakePio(), Jobs()
    configs = pio_configs(core, runner, start=jobs.append)
    empty = tmp_path / "empty"
    empty.mkdir()
    for result in (configs.cached(empty), configs.load(empty)):
        assert result is not None and result.error and not result.envs
    assert runner.calls == [] and jobs == []


def test_load_waits_and_caches(tmp_path: Path) -> None:
    root, core = _project(tmp_path)
    runner, jobs = FakePio(), Jobs()
    configs = pio_configs(core, runner, start=jobs.append)
    assert configs.core_dir == core

    loaded = configs.load(root)
    assert loaded.error is None and {n: e.board for n, e in loaded.envs.items()} == BOARDS
    assert configs.cached(root) == loaded
    assert jobs == [] and len(runner.calls) == 1
    s3 = EnvRef(root, "esp32-s3-devkitc-1")
    assert (configs.target(s3), configs.target(EnvRef(root, "esp32dev"))) == ("esp32s3", "esp32")
    env = configs.env(s3)
    assert env is not None and env.board == "esp32-s3-devkitc-1"
    other = EnvRef(root, "other")
    assert configs.env(other) is None and configs.target(other) is None


def test_target_is_unknown_while_the_query_runs(tmp_path: Path) -> None:
    root, core = _project(tmp_path)
    runner, jobs = FakePio(), Jobs()
    configs = pio_configs(core, runner, start=jobs.append)
    ref = EnvRef(root, "esp32dev")
    assert configs.target(ref) is None and configs.env(ref) is None
    assert len(jobs) == 1
    jobs.run()
    assert configs.target(ref) == "esp32"


def test_load_never_raises_for_pio_problems(tmp_path: Path) -> None:
    root, core = _project(tmp_path)
    runner = FakePio()
    runner.fail = True
    failed = pio_configs(core, runner).load(root)
    assert failed.error and not failed.envs

    not_found = PioConfigs(
        environ={"PLATFORMIO_CORE_DIR": str(core)},
        run=runner,
        which=lambda _: None,
        start=lambda job: job(),
    ).load(root)
    assert not_found.error and not not_found.envs
    assert len(runner.calls) == 1  # pio was not found, so nothing ran
