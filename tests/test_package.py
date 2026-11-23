import re
import tomllib
from pathlib import Path

import mcp_google_tasks

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_version_is_semver() -> None:
    assert isinstance(mcp_google_tasks.__version__, str)
    assert re.fullmatch(r"\d+\.\d+\.\d+", mcp_google_tasks.__version__)


def test_version_is_the_hatch_source() -> None:
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    assert "version" in project["project"]["dynamic"]
    assert project["tool"]["hatch"]["version"]["path"] == "src/mcp_google_tasks/__init__.py"
