"""M1 step 6 pin tests — no Docker needed (plan §7 tests 1–2).

The shipped image defaults must be well-formed digest refs for the right repo, and the
README's "What `setup` does" commands must quote them verbatim: a re-pin that forgets the
README would otherwise hand new users a pull that can never satisfy the preflight.
(That the digests hold python 3.12 / node 22 is checked in tests/test_container.py.)
"""
import re
from pathlib import Path

import pytest

from vestibule.config import DEFAULT_IMAGE_NODE, DEFAULT_IMAGE_PYTHON

README = Path(__file__).resolve().parents[1] / "README.md"


@pytest.mark.parametrize(
    ("ref", "repo"), [(DEFAULT_IMAGE_PYTHON, "python"), (DEFAULT_IMAGE_NODE, "node")]
)
def test_default_image_is_a_fully_qualified_digest_ref(ref, repo):
    """Per constant, full match: catches a truncated paste and a python/node swap."""
    assert re.fullmatch(rf"docker\.io/library/{repo}@sha256:[0-9a-f]{{64}}", ref), ref


@pytest.mark.parametrize("ref", [DEFAULT_IMAGE_PYTHON, DEFAULT_IMAGE_NODE])
def test_readme_pull_command_matches_config(ref):
    assert f"docker pull {ref}" in README.read_text(encoding="utf-8")


def test_readme_image_rm_line_quotes_both_refs():
    """M4 step 4 plan §2.8: the uninstall's line is pasted as is, so a re-pin must reach it."""
    lines = [s for s in README.read_text(encoding="utf-8").splitlines() if "docker image rm" in s]
    assert any(DEFAULT_IMAGE_PYTHON in s and DEFAULT_IMAGE_NODE in s for s in lines), lines
