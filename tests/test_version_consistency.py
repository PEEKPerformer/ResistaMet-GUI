"""Assert the three places we record a version number agree.

Catches the "I bumped constants.py but forgot pyproject.toml / CITATION.cff"
class of release-prep mistake — exactly what happened today between v1.5.0
and the v1.5.1 release flow.

Cheap, no GUI, no instrument, runs in the default pytest invocation.
"""
from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def test_version_strings_agree():
    # 1. resistamet_gui/constants.py
    from resistamet_gui.constants import __version__ as code_version

    # 2. pyproject.toml — match the [project] table's version line. Avoid
    #    pulling in tomllib (3.11+) so we stay compatible with the 3.9 floor
    #    declared in requires-python.
    pyproject = _read("pyproject.toml")
    m = re.search(
        r'^\s*\[project\][^[]*?^version\s*=\s*"([^"]+)"',
        pyproject, re.MULTILINE | re.DOTALL,
    )
    assert m, "pyproject.toml: could not find [project] version"
    pyproject_version = m.group(1)

    # 3. CITATION.cff — line of the form `version: X.Y.Z` (sometimes quoted).
    citation = _read("CITATION.cff")
    m = re.search(r'^version:\s*["\']?([^"\'\n]+)["\']?\s*$', citation, re.MULTILINE)
    assert m, "CITATION.cff: could not find version: line"
    citation_version = m.group(1).strip()

    assert code_version == pyproject_version == citation_version, (
        f"version mismatch: "
        f"constants.py={code_version}, "
        f"pyproject.toml={pyproject_version}, "
        f"CITATION.cff={citation_version}"
    )


def test_readme_hero_version_matches():
    """README's '**Version:** X.Y.Z' line should match constants.py.

    Not strictly load-bearing (no machine reads it), but it's the first
    thing a human visiting the repo sees, so a drift here makes the
    release look untrustworthy.
    """
    from resistamet_gui.constants import __version__ as code_version
    readme = _read("README.md")
    m = re.search(r"\*\*Version:\*\*\s*([0-9]+\.[0-9]+\.[0-9]+)", readme)
    assert m, "README: '**Version:** X.Y.Z' line not found"
    assert m.group(1) == code_version, (
        f"README hero version {m.group(1)} != constants.py {code_version}"
    )


def test_changelog_has_entry_for_current_version():
    """The Version History section of README must include an entry for the
    current __version__. Forces release-flow discipline.
    """
    from resistamet_gui.constants import __version__ as code_version
    readme = _read("README.md")
    assert f"### v{code_version}" in readme, (
        f"README Version History missing '### v{code_version}' entry"
    )
