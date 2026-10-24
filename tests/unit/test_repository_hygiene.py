"""Security/configuration checks that need no infrastructure (CI `security` job, NFR-05, AC-12)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.security]
ROOT = Path(__file__).resolve().parents[2]
SECRET_PATTERNS = {
    "aws access key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "github token": re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "slack token": re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    "password in url": re.compile(r"[a-z+]+://[^:/\s]+:(?!change-me@|x@|\[REDACTED\]|hunter2@|pw@|p@)[^@\s]{4,}@"),
}
FORBIDDEN_FILES = re.compile(r"(^|/)(\.env|\.env\.test|.*\.pem|.*\.key|id_rsa.*)$")


def _tracked() -> list[str]:
    out = subprocess.run(  # noqa: S603 - fixed argv, no shell, no user input
        ["git", "-C", str(ROOT), "ls-files"],  # noqa: S607 - git resolved from PATH
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.split()


def test_no_secret_files_tracked() -> None:
    assert [f for f in _tracked() if FORBIDDEN_FILES.search(f)] == []


def test_no_secret_patterns_in_tracked_files() -> None:
    hits = []
    this_file = Path(__file__).resolve()
    for name in _tracked():
        path = ROOT / name
        # Lockfiles hold hashes; this file defines the patterns and would match its own source.
        if path.suffix in {".lock"} or not path.is_file() or path.resolve() == this_file:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        hits += [f"{name}: {kind}" for kind, rx in SECRET_PATTERNS.items() if rx.search(text)]
    assert hits == []


def test_env_examples_hold_placeholders_only() -> None:
    for example in (".env.example", ".env.test.example"):
        for line in (ROOT / example).read_text().splitlines():
            if "://" in line and "@" in line:
                assert ":change-me@" in line, f"{example}: credentials must be the change-me placeholder"


def test_workflows_never_use_privileged_triggers_or_local_services() -> None:
    for wf in (ROOT / ".github" / "workflows").glob("*.yml"):
        text = wf.read_text()
        assert "pull_request_target" not in text
        assert "localhost:5432" not in text
        assert "localhost:6379" not in text
        assert "services:" not in text  # CP2: no shared or containerised infrastructure in CI
        for uses in re.findall(r"uses:\s*(\S+)", text):
            assert re.search(r"@[0-9a-f]{40}$", uses), f"{wf.name}: action not pinned by SHA: {uses}"
