"""No dead relative links in docs/ and examples/.

Scans every Markdown file under docs/ and examples/ (plus the top-level README and
AGENTS.md) for Markdown links and bare <...> autolinks, and asserts that every
*relative* target resolves to a file that exists. External (http/https/mailto),
in-page anchors (#...), and template placeholders (<YOUR_...>, ${...}) are skipped.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# [text](target) — target may carry a #anchor and/or a "title".
_MD_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def _md_files() -> list[Path]:
    files: list[Path] = []
    for base in ("docs", "examples"):
        files.extend((REPO / base).rglob("*.md"))
    files.append(REPO / "README.md")
    files.append(REPO / "AGENTS.md")
    return sorted(p for p in files if p.is_file())


def _targets(text: str) -> list[str]:
    out: list[str] = []
    for m in _MD_LINK.finditer(text):
        target = m.group(1).strip()
        # Drop an optional "title" after the URL: (url "title")
        if " " in target:
            target = target.split()[0]
        out.append(target)
    return out


def _is_relative(target: str) -> bool:
    if not target or target.startswith("#"):
        return False
    if re.match(r"^[a-z][a-z0-9+.-]*://", target):  # http:, https:, mailto:, etc.
        return False
    if target.startswith("mailto:") or target.startswith("tel:"):
        return False
    if target.startswith("${") or target.startswith("<"):  # placeholders
        return False
    return True


@pytest.mark.parametrize("md", _md_files(), ids=lambda p: str(p.relative_to(REPO)))
def test_relative_links_resolve(md: Path):
    broken: list[str] = []
    for target in _targets(md.read_text()):
        if not _is_relative(target):
            continue
        path_part = target.split("#", 1)[0]
        if not path_part:  # pure anchor after stripping
            continue
        resolved = (md.parent / path_part).resolve()
        if not resolved.exists():
            broken.append(target)
    assert not broken, f"{md.relative_to(REPO)} has dead relative links: {broken}"
