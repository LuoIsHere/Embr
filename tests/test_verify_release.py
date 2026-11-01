"""scripts/verify_release.py (release workflow): check() on synthetic zips, one per rule and
failure path, and main() with the worker check (a subprocess) replaced, so no real Embr.exe
is needed. The real package and the real worker check are covered by
tests/release/test_release_package.py."""

import hashlib
import importlib.util
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from embr import __version__

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "verify_release.py"
NAME = f"embr_{__version__}_windows_x64"
ZIP_NAME = f"{NAME}.zip"
BUILD_TXT = "_internal/embr/resources/build.txt"
REQUIRED = (
    "Embr.exe",
    "LICENSE",
    "THIRD_PARTY_NOTICES",
    "ESPTOOL.txt",
    "README.md",
    "CHANGELOG.md",
    BUILD_TXT,
    "_internal/embr/resources/esptool.cfg",
)
EXE = b"MZ synthetic exe"


def _load() -> Any:
    """A fresh copy of the script module (it is not a package), so patches stay local."""
    spec = importlib.util.spec_from_file_location("verify_release_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def vr() -> Any:
    return _load()


def _members() -> dict[str, bytes]:
    """The files of a good package, relative to its folder."""
    return {
        "Embr.exe": EXE,
        "LICENSE": b"GPL TEXT\r\n",
        "THIRD_PARTY_NOTICES": b"list\r\n\r\nFull license texts\r\n------------------\r\n",
        "ESPTOOL.txt": b"esptool note\r\n",
        "README.md": b"# Embr\n",
        "CHANGELOG.md": b"# Changelog\n",
        BUILD_TXT: b"2026-11-01 08:12 UTC\n",
        "_internal/embr/resources/esptool.cfg": b"[esptool]\n",
        "_internal/base_library.zip": b"PK",
    }


def _sha256sum_line(archive: Path) -> bytes:
    return f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n".encode()


def _package(
    folder: Path,
    members: dict[str, bytes] | None = None,
    *,
    name: str = ZIP_NAME,
    top: str = NAME,
    extra: dict[str, bytes] | None = None,
    checksum: bool = True,
) -> Path:
    """A zip in `folder` with `members` under `top`/ (plus `extra` entries at their own
    paths) and, unless `checksum` is False, a correct .sha256 next to it."""
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / name
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{top}/", b"")
        for rel, data in (_members() if members is None else members).items():
            zf.writestr(f"{top}/{rel}", data)
        for rel, data in (extra or {}).items():
            zf.writestr(rel, data)
    if checksum:
        Path(f"{archive}.sha256").write_bytes(_sha256sum_line(archive))
    return archive


def _without(*files: str) -> dict[str, bytes]:
    return {k: v for k, v in _members().items() if k not in files}


def _with(rel: str, data: bytes) -> dict[str, bytes]:
    return {**_members(), rel: data}


@pytest.fixture
def no_extract(monkeypatch: pytest.MonkeyPatch) -> None:
    """check() must neither extract nor run anything."""

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("check() extracted the zip or ran a process")

    monkeypatch.setattr(zipfile.ZipFile, "extractall", refuse)
    monkeypatch.setattr(zipfile.ZipFile, "extract", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)


# ---- constants ------------------------------------------------------------------------------


def test_names_and_required_files(vr: Any) -> None:
    assert vr.NAME == NAME
    assert set(vr.REQUIRED) == set(REQUIRED)
    assert vr.SCRIPTS == REPO / "scripts"
    assert (vr.SCRIPTS / "check_frozen_worker.py").is_file()


# ---- check() --------------------------------------------------------------------------------


def test_check_accepts_a_good_package(vr: Any, tmp_path: Path, no_extract: None) -> None:
    assert vr.check(_package(tmp_path)) == []


@pytest.mark.parametrize(
    "name",
    [
        f"Embr-{__version__}-windows-x64.zip",
        f"Embr_{__version__}_windows_x64.zip",
        "embr_9.9.9_windows_x64.zip",
        f"embr_{__version__}_windows_x86.zip",
        f"embr_{__version__}_windows_x64.ZIP",
        f"embr_{__version__}_windows_x64",
        f"{NAME}.zip.zip",
    ],
)
def test_check_refuses_another_name_with_that_problem_only(
    vr: Any, tmp_path: Path, name: str, no_extract: None
) -> None:
    archive = _package(tmp_path, name=name, top=name.removesuffix(".zip"), checksum=False)
    problems = vr.check(archive)
    assert len(problems) == 1, problems
    assert name in problems[0] and ZIP_NAME in problems[0]
    # also when the zip does not exist
    assert vr.check(tmp_path / "elsewhere" / name) == problems


def test_check_refuses_a_missing_zip(vr: Any, tmp_path: Path, no_extract: None) -> None:
    problems = vr.check(tmp_path / ZIP_NAME)
    assert len(problems) == 1, problems
    assert "not found" in problems[0]
    (tmp_path / ZIP_NAME).mkdir()  # a folder of that name is not the zip
    assert len(vr.check(tmp_path / ZIP_NAME)) == 1


@pytest.mark.parametrize(
    "extra",
    [
        "stray.txt",
        "Embr.exe",
        "other/README.md",
        f"{NAME}_extra/Embr.exe",
        f"Embr-{__version__}/Embr.exe",
    ],
)
def test_check_refuses_entries_outside_the_folder(
    vr: Any, tmp_path: Path, extra: str, no_extract: None
) -> None:
    problems = vr.check(_package(tmp_path, extra={extra: b"x"}))
    assert len(problems) == 1, problems
    assert "outside" in problems[0] and extra in problems[0]


def test_check_refuses_a_package_in_another_folder(
    vr: Any, tmp_path: Path, no_extract: None
) -> None:
    problems = vr.check(_package(tmp_path, top=f"Embr-{__version__}"))
    assert any("outside" in p for p in problems), problems
    for file in REQUIRED:
        assert any(p.startswith(file) and "missing" in p for p in problems), file


@pytest.mark.parametrize("file", REQUIRED)
def test_check_refuses_a_missing_required_file(
    vr: Any, tmp_path: Path, file: str, no_extract: None
) -> None:
    problems = vr.check(_package(tmp_path, _without(file)))
    assert problems == [f"{file} missing"]


def test_check_wants_the_required_files_under_the_folder(
    vr: Any, tmp_path: Path, no_extract: None
) -> None:
    """At the top of the zip instead of under <name>/ they do not count."""
    archive = _package(tmp_path, _without(*REQUIRED), extra=dict(_members()))
    problems = vr.check(archive)
    for file in REQUIRED:
        assert f"{file} missing" in problems
    assert any("outside" in p for p in problems)


@pytest.mark.parametrize(
    "text",
    [
        b"2026-11-01 08:12 UTC",
        b"2026-11-01 08:12 UTC\n",
        b"2026-11-01 08:12 UTC\r\n",
        b"  2026-11-01 08:12 UTC  \n\n",
        b"0000-99-99 99:99 UTC",
    ],
)
def test_check_accepts_build_txt_in_the_build_time_format(
    vr: Any, tmp_path: Path, text: bytes, no_extract: None
) -> None:
    assert vr.check(_package(tmp_path, _with(BUILD_TXT, text))) == []


@pytest.mark.parametrize(
    "text",
    [
        b"",
        b"\n",
        b"development build",
        b"2026-11-01 08:12",
        b"2026-11-01 08:12 utc",
        b"2026-11-01 08:12 GMT",
        b"2026-11-01 08:12:33 UTC",
        b"2026-11-1 08:12 UTC",
        b"2026-11-01 8:12 UTC",
        b"26-11-01 08:12 UTC",
        b"2026-11-01T08:12 UTC",
        b"2026/11/01 08:12 UTC",
        b"2026-11-01  08:12 UTC",
        b"2026-11-01 08:12 UTC extra",
        b"x2026-11-01 08:12 UTC",
        b"2026-11-01 08:12 UTC\n2026-11-01 08:13 UTC",
    ],
)
def test_check_refuses_build_txt_in_another_format(
    vr: Any, tmp_path: Path, text: bytes, no_extract: None
) -> None:
    problems = vr.check(_package(tmp_path, _with(BUILD_TXT, text)))
    assert len(problems) == 1, problems
    assert "build.txt" in problems[0]


@pytest.mark.parametrize(
    "text",
    [b"list only\r\n", b"list\r\n\r\nfull license texts\r\n", b"Full  license texts", b""],
    ids=["no-section", "lower-case", "two-spaces", "empty"],
)
def test_check_refuses_notices_without_the_full_texts(
    vr: Any, tmp_path: Path, text: bytes, no_extract: None
) -> None:
    problems = vr.check(_package(tmp_path, _with("THIRD_PARTY_NOTICES", text)))
    assert len(problems) == 1, problems
    assert "THIRD_PARTY_NOTICES" in problems[0]


def test_check_refuses_a_missing_checksum(vr: Any, tmp_path: Path, no_extract: None) -> None:
    problems = vr.check(_package(tmp_path, checksum=False))
    assert problems == [f"{ZIP_NAME}.sha256 missing"]


def _checksum_variants(archive: Path) -> dict[str, bytes]:
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    return {
        "crlf": f"{digest}  {ZIP_NAME}\r\n".encode(),
        "no-newline": f"{digest}  {ZIP_NAME}".encode(),
        "two-newlines": f"{digest}  {ZIP_NAME}\n\n".encode(),
        "one-space": f"{digest} {ZIP_NAME}\n".encode(),
        "binary-marker": f"{digest} *{ZIP_NAME}\n".encode(),
        "upper-case": f"{digest.upper()}  {ZIP_NAME}\n".encode(),
        "wrong-hash": f"{'0' * 64}  {ZIP_NAME}\n".encode(),
        "md5-length": f"{digest[:32]}  {ZIP_NAME}\n".encode(),
        "old-name": f"{digest}  Embr-{__version__}-windows-x64.zip\n".encode(),
        "with-folder": f"{digest}  dist/release/{ZIP_NAME}\n".encode(),
        "hash-only": f"{digest}\n".encode(),
        "bom": b"\xef\xbb\xbf" + f"{digest}  {ZIP_NAME}\n".encode(),
        "empty": b"",
    }


CHECKSUM_VARIANTS = list(_checksum_variants(Path(SCRIPT)))


@pytest.mark.parametrize("variant", CHECKSUM_VARIANTS)
def test_check_refuses_a_checksum_not_in_sha256sum_form(
    vr: Any, tmp_path: Path, variant: str, no_extract: None
) -> None:
    archive = _package(tmp_path)
    Path(f"{archive}.sha256").write_bytes(_checksum_variants(archive)[variant])
    problems = vr.check(archive)
    assert problems == [f"{ZIP_NAME}.sha256 does not match the zip"]


def test_check_refuses_a_checksum_of_an_earlier_zip(
    vr: Any, tmp_path: Path, no_extract: None
) -> None:
    archive = _package(tmp_path)
    stale = Path(f"{archive}.sha256").read_bytes()
    _package(tmp_path, _with("README.md", b"# Embr, changed\n"))
    Path(f"{archive}.sha256").write_bytes(stale)
    assert vr.check(archive) == [f"{ZIP_NAME}.sha256 does not match the zip"]


def test_check_reports_every_problem(vr: Any, tmp_path: Path, no_extract: None) -> None:
    members = {**_without("ESPTOOL.txt"), BUILD_TXT: b"development build\n"}
    members["THIRD_PARTY_NOTICES"] = b"list only\n"
    archive = _package(tmp_path, members, extra={"stray.txt": b"x"}, checksum=False)
    problems = vr.check(archive)
    assert len(problems) == 5, problems
    assert any("outside" in p for p in problems)
    assert "ESPTOOL.txt missing" in problems
    assert any("build.txt" in p for p in problems)
    assert any("THIRD_PARTY_NOTICES" in p for p in problems)
    assert f"{ZIP_NAME}.sha256 missing" in problems


# ---- main() ---------------------------------------------------------------------------------


@dataclass
class _Worker:
    """Stands in for subprocess.run of the worker check: records each command and what the
    exe it names held at that moment."""

    returncode: int = 0
    calls: list[list[str]] = field(default_factory=list[list[str]])
    seen: list[bytes | None] = field(default_factory=list[bytes | None])

    def __call__(self, command: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(command))
        exe = Path(command[-1])
        self.seen.append(exe.read_bytes() if exe.is_file() else None)
        assert options.get("check") is not True, "a failed worker check must be reported"
        return subprocess.CompletedProcess(command, self.returncode)


@pytest.fixture
def worker(vr: Any, monkeypatch: pytest.MonkeyPatch) -> _Worker:
    fake = _Worker()
    monkeypatch.setattr(vr.subprocess, "run", fake)
    return fake


def _main(vr: Any, monkeypatch: pytest.MonkeyPatch, *args: str) -> int:
    monkeypatch.setattr(sys, "argv", ["verify_release.py", *args])
    return vr.main()


@pytest.mark.parametrize("args", [(), ("a.zip", "b.zip")], ids=["none", "two"])
def test_main_usage_on_a_wrong_argument_count(
    vr: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    worker: _Worker,
    args: Any,
) -> None:
    assert _main(vr, monkeypatch, *args) == 2
    assert capsys.readouterr().out.startswith("usage: verify_release.py <zip>")
    assert worker.calls == []


def test_main_runs_the_worker_check_on_the_extracted_exe(
    vr: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    worker: _Worker,
) -> None:
    archive = _package(tmp_path / "release")
    before = sorted(p.name for p in archive.parent.iterdir())
    assert _main(vr, monkeypatch, str(archive)) == 0
    assert capsys.readouterr().out == f"ok: {ZIP_NAME}\n"
    assert len(worker.calls) == 1
    command = worker.calls[0]
    assert command[:2] == [sys.executable, str(REPO / "scripts" / "check_frozen_worker.py")]
    assert len(command) == 3
    exe = Path(command[2])
    assert exe.is_absolute()
    assert exe.name == "Embr.exe" and exe.parent.name == NAME
    assert worker.seen == [EXE], "Embr.exe was not extracted from the zip before the check"
    assert archive.parent not in exe.parents, "extracted next to the zip"
    assert not exe.parent.parent.exists(), "the extraction folder was left behind"
    assert sorted(p.name for p in archive.parent.iterdir()) == before


def test_main_accepts_a_relative_path(
    vr: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    worker: _Worker,
) -> None:
    _package(tmp_path / "dist" / "release")
    monkeypatch.chdir(tmp_path)
    assert _main(vr, monkeypatch, f"dist/release/{ZIP_NAME}") == 0
    assert capsys.readouterr().out == f"ok: {ZIP_NAME}\n"
    assert worker.seen == [EXE]


def test_main_fails_when_the_worker_check_fails(
    vr: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    worker: _Worker,
) -> None:
    worker.returncode = 1
    assert _main(vr, monkeypatch, str(_package(tmp_path))) == 1
    out = capsys.readouterr().out
    assert out.startswith("FAIL: ") and "worker check failed" in out
    assert "ok:" not in out
    assert len(worker.calls) == 1


@pytest.mark.parametrize(
    "broken",
    ["name", "missing-file", "build-txt", "notices", "checksum", "outside"],
)
def test_main_reports_the_problems_without_running_the_worker_check(
    vr: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    worker: _Worker,
    broken: str,
) -> None:
    if broken == "name":
        archive = _package(tmp_path, name=f"Embr-{__version__}-windows-x64.zip")
    elif broken == "missing-file":
        archive = _package(tmp_path, _without("README.md"))
    elif broken == "build-txt":
        archive = _package(tmp_path, _with(BUILD_TXT, b"development build"))
    elif broken == "notices":
        archive = _package(tmp_path, _with("THIRD_PARTY_NOTICES", b"list only"))
    elif broken == "outside":
        archive = _package(tmp_path, extra={"stray.txt": b"x"})
    else:
        archive = _package(tmp_path)
        Path(f"{archive}.sha256").write_bytes(_checksum_variants(archive)["crlf"])
    problems = vr.check(archive)
    assert len(problems) == 1
    assert _main(vr, monkeypatch, str(archive)) == 1
    assert capsys.readouterr().out == f"FAIL: {problems[0]}\n"
    assert worker.calls == []


def test_main_prints_each_problem(
    vr: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    worker: _Worker,
) -> None:
    archive = _package(tmp_path, _without("LICENSE", "CHANGELOG.md"), checksum=False)
    assert _main(vr, monkeypatch, str(archive)) == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        "FAIL: LICENSE missing",
        "FAIL: CHANGELOG.md missing",
        f"FAIL: {ZIP_NAME}.sha256 missing",
    ]
    assert worker.calls == []


def test_script_exit_codes(tmp_path: Path) -> None:
    """Run as the workflow runs it (failure paths only: the success path needs a real
    Embr.exe and is in the release suite)."""

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    usage = run()
    assert usage.returncode == 2 and usage.stdout.startswith("usage:")
    archive = _package(tmp_path, checksum=False)
    failed = run(str(archive))
    assert failed.returncode == 1
    assert failed.stdout == f"FAIL: {ZIP_NAME}.sha256 missing\n"
    absent = run(str(tmp_path / "nothing" / ZIP_NAME))
    assert absent.returncode == 1 and "not found" in absent.stdout
