"""The entry-point scripts must be runnable on their own.

`uv run gsc_worker.py` resolves dependencies from the script's PEP-723 header,
not from `pyproject.toml`, so the two lists can drift silently: the package
imports fine under pytest while the worker a client actually launches dies at
import. These tests keep them in step.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from vgi_google_search_console import __version__

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ("gsc_worker.py", "serve.py")

_BLOCK = re.compile(r"^# /// script$(.+?)^# ///$", re.MULTILINE | re.DOTALL)


def _name(requirement: str) -> str:
    return re.split(r"[\[><=!~;\s]", requirement, maxsplit=1)[0].strip().lower()


def _script_requirements(path: Path) -> list[str]:
    match = _BLOCK.search(path.read_text())
    assert match is not None, f"{path.name} has no PEP-723 script header"
    body = "".join(line.removeprefix("# ").removeprefix("#") for line in match.group(1).splitlines(True))
    return list(tomllib.loads(body)["dependencies"])


def _project() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]


class TestScriptHeaders:
    @pytest.mark.parametrize("script", SCRIPTS)
    def test_header_covers_every_runtime_dependency(self, script: str) -> None:
        declared = {_name(r) for r in _script_requirements(ROOT / script)}
        missing = {_name(r) for r in _project()["dependencies"]} - declared
        assert missing == set(), f"{script} would fail at import: missing {sorted(missing)}"

    @pytest.mark.parametrize("script", SCRIPTS)
    def test_header_declares_nothing_unknown(self, script: str) -> None:
        extra = (
            {_name(r) for r in _script_requirements(ROOT / script)}
            - {_name(r) for r in _project()["dependencies"]}
            - {"vgi-rpc"}
        )
        assert extra == set(), extra

    @pytest.mark.parametrize("script", SCRIPTS)
    def test_header_pins_match_pyproject(self, script: str) -> None:
        """Same lower bounds in both places, so a bump cannot land in only one."""
        project = {_name(r): r for r in _project()["dependencies"]}
        for requirement in _script_requirements(ROOT / script):
            if _name(requirement) in project:
                assert requirement == project[_name(requirement)], (script, requirement)


class TestFilterEngine:
    """vgi-python evaluates pushed filters with an in-process DuckDB; the worker must ask for one."""

    def test_every_vgi_python_requirement_has_the_haybarn_extra(self) -> None:
        specs = [r for r in _project()["dependencies"] if _name(r) == "vgi-python"]
        for extra in _project().get("optional-dependencies", {}).values():
            specs += [r for r in extra if _name(r) == "vgi-python"]
        for script in SCRIPTS:
            specs += [r for r in _script_requirements(ROOT / script) if _name(r) == "vgi-python"]
        assert specs
        for spec in specs:
            assert "haybarn" in spec, spec


class TestMetadata:
    def test_version_is_single_sourced(self) -> None:
        assert _project()["dynamic"] == ["version"]
        assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)

    def test_license_is_machine_detectable(self) -> None:
        assert _project()["license"] == "MIT"
        assert (ROOT / "LICENSE").read_text().startswith("MIT License")

    def test_changelog_mentions_this_version(self) -> None:
        assert f"## {__version__}" in (ROOT / "CHANGELOG.md").read_text()
