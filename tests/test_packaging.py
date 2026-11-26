"""Catalog-compliance checks on the shipped metadata and README (RET-185).

The Hermes plugin guide and the catalog admission rules ask for things no unit of plugin
code exercises: a masked install prompt for secrets, a pyproject that Hermes's package
manager treats as "installs nothing", truthful versions, and README disclosures (rule 13).
"""

from __future__ import annotations

import importlib
import re
import tomllib

import pytest

from conftest import REPO_ROOT
from hermes_yaml import safe_load

from hermes_linear.constants import PLUGIN_VERSION

MANIFEST = safe_load((REPO_ROOT / "plugin.yaml").read_text())
PYPROJECT = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
README = (REPO_ROOT / "README.md").read_text()


def test_required_secrets_are_masked_at_install():
    # `hermes plugins install` masks the prompt only on `secret: true`
    # (hermes_cli/plugins_cmd_install.py); `password: true` is the gateway setup UI's key.
    for spec in MANIFEST["requires_env"]:
        assert spec.get("secret") is True, spec["name"]
        assert spec.get("password") is True, spec["name"]


def test_versions_agree():
    assert str(MANIFEST["version"]) == PYPROJECT["project"]["version"] == PLUGIN_VERSION


def test_manifest_v2_metadata():
    assert MANIFEST["license"] == "MIT"
    assert MANIFEST["homepage"] == "https://github.com/masha256/hermes-linear"
    assert {"gateway", "linear"} <= set(MANIFEST["tags"])


def test_pyproject_is_not_an_install_declaration():
    """A pyproject with [build-system] makes Hermes's PM build the plugin as an editable
    package into every environment generation, even with no dependencies."""
    assert "build-system" not in PYPROJECT
    assert "entry-points" not in PYPROJECT["project"]
    assert PYPROJECT["project"]["dependencies"] == []
    try:
        decl = importlib.import_module("pm.plugin_declarations")
    except ImportError:
        pytest.skip("pm.plugin_declarations not in this Hermes")
    read = getattr(decl, "read_python_declaration", None)
    if read is None:
        pytest.skip("read_python_declaration not in this Hermes")
    result = read(REPO_ROOT)
    if not hasattr(result, "pyproject_installs"):
        pytest.skip("PythonDeclaration has no pyproject_installs in this Hermes")
    assert result.pyproject_installs is False
    assert tuple(result.install_requirements) == ()


def test_readme_not_affiliated_with_linear():
    assert re.search(r"not affiliated with Linear", README)
    assert "not an official" in README


@pytest.mark.parametrize("needle", [
    "api.linear.app",
    "/oauth/token",
    "storage.googleapis.com",
    "tailscale status --json",
    "hermes linear test",
    "plugin-data/linear/data.db",
    "`git`",
    "plugins.isolation: host",
])
def test_readme_discloses(needle):
    assert needle in README, needle


def test_readme_screenshots_exist():
    shots = re.findall(r"\]\((docs/screenshots/[^)]+)\)", README)
    assert len(shots) == 4
    for rel in shots:
        assert (REPO_ROOT / rel).is_file(), rel
