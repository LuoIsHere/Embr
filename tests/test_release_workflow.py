""".github/workflows/release.yml: what the release workflow guarantees, read from its YAML.

A version tag runs the same check as CI plus the tag/version check, then packages, verifies
and publishes; a manual run does all but publishing. Only the release job may write to the
repository, it runs only for tags, the tag reaches scripts only through the environment,
no step runs the release or hardware suites, actions are pinned like ci.yml, and the
artifact holds exactly the files package_release.py writes."""

import fnmatch
import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github" / "workflows"
TAG_ONLY = "github.ref_type == 'tag'"
ACTIONS = {
    "actions/checkout": "v7",
    "astral-sh/setup-uv": "v10.2.0",
    "actions/upload-artifact": "v7.0.2",
    "actions/download-artifact": "v8.0.2",
}


def _yaml(name: str) -> dict[Any, Any]:
    loaded: Any = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded  # pyright: ignore[reportUnknownVariableType]


@pytest.fixture(scope="module")
def release() -> dict[str, Any]:
    return _yaml("release.yml")


@pytest.fixture(scope="module")
def ci() -> dict[str, Any]:
    return _yaml("ci.yml")


def _package_name() -> str:
    spec = importlib.util.spec_from_file_location(
        "package_release_for_workflow", REPO / "scripts" / "package_release.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.PACKAGE_NAME


def _triggers(doc: dict[Any, Any]) -> dict[str, Any]:
    """`on:` (YAML 1.1 reads the bare key as True)."""
    triggers = doc.get("on", doc.get(True))
    assert isinstance(triggers, dict), triggers
    return triggers  # pyright: ignore[reportUnknownVariableType]


def _jobs(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return doc["jobs"]


def _steps(doc: dict[str, Any], job: str) -> list[dict[str, Any]]:
    return _jobs(doc)[job]["steps"]


def _needs(job: dict[str, Any]) -> list[str]:
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)  # pyright: ignore[reportUnknownArgumentType]


def _all_steps(doc: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [(name, step) for name, job in _jobs(doc).items() for step in job["steps"]]


def _runs(doc: dict[str, Any]) -> list[tuple[str, str]]:
    return [(job, step["run"]) for job, step in _all_steps(doc) if "run" in step]


def _what(step: dict[str, Any]) -> dict[str, Any]:
    """A step without its display name."""
    return {k: v for k, v in step.items() if k != "name"}


def _is_version_check(step: dict[str, Any]) -> bool:
    return "scripts/check_version.py" in step.get("run", "")


def _index(steps: list[dict[str, Any]], text: str) -> int:
    found = [i for i, s in enumerate(steps) if text in s.get("run", "")]
    assert len(found) == 1, f"{text!r} is run {len(found)} times"
    return found[0]


# ---- triggers, permissions, jobs ----------------------------------------------------------


def test_runs_on_version_tags_and_by_hand_only(release: dict[str, Any]) -> None:
    triggers = _triggers(release)
    assert set(triggers) == {"push", "workflow_dispatch"}
    assert triggers["push"] == {"tags": ["v*.*.*"]}, "pushes other than version tags"
    assert not triggers["workflow_dispatch"], "manual runs take no inputs"


def test_only_the_release_job_may_write(release: dict[str, Any]) -> None:
    assert release["permissions"] == {"contents": "read"}
    jobs = _jobs(release)
    assert jobs["release"]["permissions"] == {"contents": "write"}
    for name, job in jobs.items():
        if name != "release":
            assert "permissions" not in job, f"{name} changes the permissions"


def test_jobs_run_check_then_package_then_release(release: dict[str, Any]) -> None:
    jobs = _jobs(release)
    assert set(jobs) == {"check", "package", "release"}
    assert _needs(jobs["check"]) == []
    assert _needs(jobs["package"]) == ["check"]
    assert _needs(jobs["release"]) == ["package"]
    assert jobs["check"]["runs-on"] == "windows-latest"
    assert jobs["package"]["runs-on"] == "windows-latest"
    assert jobs["release"]["runs-on"] == "ubuntu-latest"


def test_release_only_for_tags(release: dict[str, Any]) -> None:
    jobs = _jobs(release)
    assert jobs["release"]["if"] == TAG_ONLY
    for name in ("check", "package"):
        assert "if" not in jobs[name], f"{name} must also run by hand"
    publishing = [job for job, run in _runs(release) if "gh release" in run]
    assert publishing == ["release"], "only the release job publishes"


# ---- the check job ---------------------------------------------------------------------------


def test_check_job_is_ci_check_plus_the_version_check(
    release: dict[str, Any], ci: dict[str, Any]
) -> None:
    steps = _steps(release, "check")
    version = [s for s in steps if _is_version_check(s)]
    assert len(version) == 1
    others = [_what(s) for s in steps if not _is_version_check(s)]
    assert others == [_what(s) for s in _steps(ci, "check")]
    assert _jobs(release)["check"]["runs-on"] == _jobs(ci)["check"]["runs-on"]
    runs = [s.get("run") for s in steps]
    assert "uv run pytest" in runs, "the check job does not run the tests"
    # right after the environment is set up, before the slow checks
    assert runs.index("uv sync --locked") + 1 == steps.index(version[0])


def test_version_check_for_tags_fed_through_the_environment(release: dict[str, Any]) -> None:
    (step,) = [s for s in _steps(release, "check") if _is_version_check(s)]
    assert step["if"] == TAG_ONLY
    assert step["env"] == {"TAG": "${{ github.ref_name }}"}
    assert step["run"].strip() == 'uv run python scripts/check_version.py "$TAG"'
    assert [job for job, run in _runs(release) if "check_version.py" in run] == ["check"]


def test_no_expression_is_interpolated_into_a_script(release: dict[str, Any]) -> None:
    """Values such as the tag reach scripts as environment variables only."""
    for job, run in _runs(release):
        assert "${{" not in run, f"{job}: {run!r}"


@pytest.mark.parametrize("name", ["release.yml", "ci.yml"])
def test_no_step_runs_the_release_or_hardware_suites(name: str) -> None:
    doc = _yaml(name)
    for job, run in _runs(doc):
        for word in ("tests/release", "tests\\release", "EMBR_RELEASE", "EMBR_TEST_", "hardware"):
            assert word not in run, f"{name} {job}: {run!r}"
        if "pytest" in run:
            assert run.strip() == "uv run pytest", f"{name} {job}: {run!r}"
    envs: list[dict[str, Any]] = [doc.get("env") or {}]
    for job in _jobs(doc).values():
        envs.append(job.get("env") or {})
        envs.extend(step.get("env") or {} for step in job["steps"])
    for env in envs:
        for key in env:
            assert not key.startswith(("EMBR_RELEASE", "EMBR_TEST_")), f"{name}: {key}"


# ---- actions -------------------------------------------------------------------------------


def test_actions_are_pinned_like_ci(release: dict[str, Any], ci: dict[str, Any]) -> None:
    used = {step["uses"] for _, step in _all_steps(release) if "uses" in step}
    assert used == {f"{action}@{ref}" for action, ref in ACTIONS.items()}
    in_ci = {step["uses"] for _, step in _all_steps(ci) if "uses" in step}
    shared = {u.split("@")[0] for u in in_ci} & {u.split("@")[0] for u in used}
    assert shared == {"actions/checkout", "astral-sh/setup-uv"}
    assert {u for u in used if u.split("@")[0] in shared} == in_ci
    for doc in (ci, release):
        uv = [s for _, s in _all_steps(doc) if s.get("uses", "").startswith("astral-sh/")]
        assert len(uv) == 2
        for step in uv:
            assert step["with"] == {"version": "0.13.5"}, "uv is not the version ci.yml uses"


def test_every_job_works_on_a_checkout(release: dict[str, Any]) -> None:
    for name in _jobs(release):
        assert _steps(release, name)[0]["uses"].startswith("actions/checkout@"), name


# ---- package and release -------------------------------------------------------------------


def _expand(run: str) -> str:
    """The script with each $(uv run python -c '...') replaced by what it prints."""

    def output(match: re.Match[str]) -> str:
        done = subprocess.run(
            [sys.executable, "-c", match[1]],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        return done.stdout.strip()

    return re.sub(r"\$\(uv run python -c '([^']*)'\)", output, run)


def test_package_job_builds_checks_and_verifies(release: dict[str, Any]) -> None:
    steps = _steps(release, "package")
    runs = [s.get("run", "") for s in steps]
    assert next(s["run"].strip() for s in steps if "run" in s) == "uv sync --locked"
    build = _index(steps, "scripts/package_release.py")
    assert runs[build].strip() == "uv run python scripts/package_release.py", "not a full build"
    worker = _index(steps, "scripts/check_frozen_worker.py")
    assert runs[worker].strip() == (
        "uv run python scripts/check_frozen_worker.py dist/Embr/Embr.exe"
    )
    verify = _index(steps, "scripts/verify_release.py")
    upload = [i for i, s in enumerate(steps) if s.get("uses", "").startswith("actions/upload-")]
    assert len(upload) == 1
    assert build < worker < verify < upload[0]


def test_package_job_verifies_the_zip_package_release_writes(release: dict[str, Any]) -> None:
    steps = _steps(release, "package")
    script = _expand(steps[_index(steps, "scripts/verify_release.py")]["run"])
    variables = dict(re.findall(r'^\s*(\w+)="([^"$]*)"\s*$', script, re.MULTILINE))
    (command,) = [line for line in script.splitlines() if "verify_release.py" in line]
    path = re.fullmatch(r'\s*uv run python scripts/verify_release.py "([^"]+)"\s*', command)
    assert path, command
    expanded = re.sub(r"\$(\w+)", lambda m: variables[m[1]], path[1])
    assert expanded == f"dist/release/{_package_name()}.zip"


def test_artifact_holds_the_zip_and_its_checksum(release: dict[str, Any]) -> None:
    (upload,) = [s for s in _steps(release, "package") if "upload-artifact" in s.get("uses", "")]
    options = upload["with"]
    assert options["name"] == "release"
    assert options["if-no-files-found"] == "error"
    globs = [line.strip() for line in options["path"].splitlines() if line.strip()]
    name = _package_name()
    written = [f"dist/release/{name}.zip", f"dist/release/{name}.zip.sha256"]
    for file in written:
        assert any(fnmatch.fnmatchcase(file, g) for g in globs), f"{file} not uploaded"
    for glob in globs:
        assert [f for f in written if fnmatch.fnmatchcase(f, glob)], f"{glob} matches nothing"
    others = [f"dist/release/{name}", "dist/Embr/Embr.exe", f"dist/release/Embr-{name}.zip"]
    assert not any(fnmatch.fnmatchcase(f, g) for f in others for g in globs)


def test_release_job_publishes_the_artifact(release: dict[str, Any]) -> None:
    steps = _steps(release, "release")
    (download,) = [s for s in steps if "download-artifact" in s.get("uses", "")]
    assert download["with"] == {"name": "release", "path": "release"}
    (publish,) = [s for s in steps if "gh release create" in s.get("run", "")]
    assert steps.index(download) < steps.index(publish)
    assert publish["env"] == {"GH_TOKEN": "${{ github.token }}", "TAG": "${{ github.ref_name }}"}
    run = publish["run"]
    create = re.search(r"gh release create (.*?)(?<!\\)$", run, re.MULTILINE | re.DOTALL)
    assert create, run
    command = create[0].replace("\\\n", " ")
    assert command.split()[3] == '"$TAG"'
    assert "release/*.zip" in command.split()
    assert "release/*.sha256" in command.split()
    assert "--verify-tag" in command.split()
    assert "--notes-file" in command.split()
    notes = command.split()[command.split().index("--notes-file") + 1]
    before = run[: run.index("gh release create")]
    assert f"> {notes}" in before
    assert "cat CHANGELOG.md" in before and "cat release/*.sha256" in before
