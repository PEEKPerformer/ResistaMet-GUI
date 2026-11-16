"""Assert the places we record a version number agree.

The code's version is the one in ``constants.py``: it is what a data file's
``software_version`` says wrote it. ``pyproject.toml`` and the README repeat
it, and the desktop app carries the same version in its own spelling.

``CITATION.cff`` is not tied to it. A citation names an archived release, and
the code on the branch is ahead of the last release most of the time: while
the two were held equal, 2.0 development builds stamped their files with a
1.x version, the same one a different program had written.

Cheap, no GUI, no instrument, runs in the default pytest invocation.
"""
from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def _pyproject_version() -> str:
    # Match the [project] table's version line. Avoid pulling in tomllib
    # (3.11+) so we stay compatible with the 3.9 floor in requires-python.
    m = re.search(
        r'^\s*\[project\][^[]*?^version\s*=\s*"([^"]+)"',
        _read("pyproject.toml"), re.MULTILINE | re.DOTALL,
    )
    assert m, "pyproject.toml: could not find [project] version"
    return m.group(1)


def _released_versions():
    """Versions with a dated CHANGELOG heading, newest first."""
    return re.findall(r"^##\s+\[(\d+\.\d+\.\d+)\]\s+-\s+\d{4}-\d{2}-\d{2}",
                      _read("CHANGELOG.md"), re.MULTILINE)


def _is_prerelease(version: str) -> bool:
    return re.fullmatch(r"\d+\.\d+\.\d+", version) is None


def test_code_and_package_versions_agree():
    from resistamet_gui.constants import __version__ as code_version
    assert code_version == _pyproject_version()


def test_citation_names_a_release():
    """CITATION.cff cites an archived release: the newest one in the
    changelog, or the code's own version when that is itself a release."""
    from resistamet_gui.constants import __version__ as code_version
    m = re.search(r'^version:\s*["\']?([^"\'\n]+)["\']?\s*$', _read("CITATION.cff"), re.MULTILINE)
    assert m, "CITATION.cff: could not find version: line"
    cited = m.group(1).strip()
    if _is_prerelease(code_version):
        assert cited == _released_versions()[0], (
            f"CITATION.cff cites {cited}; the newest release in CHANGELOG.md is "
            f"{_released_versions()[0]}")
    else:
        assert cited == code_version


def test_desktop_app_has_the_backends_version():
    """The desktop app spells a pre-release the semver way: backend
    ``2.0.0rc4`` is desktop ``2.0.0-4``. Its builds bundle this backend, and a
    file's ``software_version`` and ``client.version`` should name one build."""
    import json
    from resistamet_gui.constants import __version__ as code_version
    desktop = json.loads(_read("desktop/src-tauri/tauri.conf.json"))["version"]
    m = re.fullmatch(r"(\d+\.\d+\.\d+)(?:rc(\d+))?", code_version)
    assert m, f"unexpected backend version {code_version}"
    expected = m.group(1) if m.group(2) is None else f"{m.group(1)}-{m.group(2)}"
    assert desktop == expected, f"desktop {desktop} != backend {code_version}"
    for name in ("desktop/package.json", "desktop/src-tauri/Cargo.toml"):
        assert desktop in _read(name), f"{name} does not carry {desktop}"


def test_readme_hero_version_matches():
    """README's '**Version:** X.Y.Z' line should match constants.py.

    Not strictly load-bearing (no machine reads it), but it's the first
    thing a human visiting the repo sees, so a drift here makes the
    release look untrustworthy.
    """
    from resistamet_gui.constants import __version__ as code_version
    readme = _read("README.md")
    m = re.search(r"\*\*Version:\*\*\s*(\S+)", readme)
    assert m, "README: '**Version:** X.Y.Z' line not found"
    assert m.group(1) == code_version, (
        f"README hero version {m.group(1)} != constants.py {code_version}"
    )


def test_changelog_has_entry_for_current_version():
    """CHANGELOG.md must include an entry for the current __version__.
    Forces release-flow discipline.
    """
    from resistamet_gui.constants import __version__ as code_version
    changelog = _read("CHANGELOG.md")
    # Keep-a-Changelog uses `## [X.Y.Z] - DATE`; accept either the bracketed
    # form or a plain heading so a temporary unreleased entry is also valid.
    if _is_prerelease(code_version):
        # A pre-release has no dated entry yet; its changes are the
        # Unreleased section.
        assert re.search(r"^##\s+\[Unreleased\]", changelog, re.MULTILINE)
        return
    pattern = rf"^##\s+\[?{re.escape(code_version)}\]?"
    assert re.search(pattern, changelog, re.MULTILINE), (
        f"CHANGELOG.md missing entry for {code_version}"
    )
