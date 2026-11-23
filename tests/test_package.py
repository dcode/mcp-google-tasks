"""Tests for package metadata: the version source and every place that repeats the version."""

import re
import tomllib
from pathlib import Path

import pytest

import mcp_google_tasks

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"


def test_version_is_semver() -> None:
    assert isinstance(mcp_google_tasks.__version__, str)
    assert re.fullmatch(r"\d+\.\d+\.\d+", mcp_google_tasks.__version__)


def test_version_is_the_hatch_source() -> None:
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    assert "version" in project["project"]["dynamic"]
    assert project["tool"]["hatch"]["version"]["path"] == "src/mcp_google_tasks/__init__.py"


# Files that pin the released image by version. `hatch version` changes only __init__.py,
# so a bump must update these too; the release workflow runs this test before publishing.
@pytest.mark.parametrize(
    ("path", "pattern"),
    [
        ("catalog/google-tasks.yaml", r"image: ghcr\.io/dcode/mcp-google-tasks:(\S+)"),
        ("contrib/kubernetes/base/kustomization.yaml", r"newTag: (\S+)"),
    ],
)
def test_pinned_image_tag_matches_version(path: str, pattern: str) -> None:
    pins = re.findall(pattern, (ROOT / path).read_text(encoding="utf-8"))
    assert pins == [mcp_google_tasks.__version__], f"{path} pins {pins}; set it to __version__"
