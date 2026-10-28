from importlib.metadata import version

import pytest

import embr
from embr.main import main


def test_version_matches_package_metadata() -> None:
    assert embr.__version__ == version("embr")


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"Embr {embr.__version__}"
