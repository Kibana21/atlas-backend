"""Security/configuration checks that need no infrastructure (CI `security` job, NFR-05, AC-12)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.unit, pytest.mark.security]
ROOT = Path(__file__).resolve().parents[2]
SECRET_PATTERNS = {
    "aws access key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "github token": re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "slack token": re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    # Allowed: documented placeholders, and ${VAR} references resolved at run time (never literals).
    "password in url": re.compile(
        r"[a-z+]+://[^:/\s]+:(?!change-me@|x@|\[REDACTED\]|hunter2@|pw@|p@|\$\{[A-Za-z_][A-Za-z0-9_]*\}@)[^@\s]{4,}@"
    ),
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
    """CI never reaches the owner's services: localhost:5432/6379 only via the job's own pinned services."""
    for wf in (ROOT / ".github" / "workflows").glob("*.yml"):
        text = wf.read_text()
        assert "pull_request_target" not in text
        for name, job in yaml.safe_load(text)["jobs"].items():
            services = job.get("services") or {}
            assert set(services) <= {"postgres", "redis"}, f"{wf.name}/{name}: unexpected services {sorted(services)}"
            if "postgres" in services:
                pg = services["postgres"]
                assert re.fullmatch(r"postgres:15(\.\d+)?@sha256:[0-9a-f]{64}", pg["image"]), pg["image"]
                assert pg["env"]["POSTGRES_HOST_AUTH_METHOD"] == "scram-sha-256"  # FR-39
                assert "--auth-local=scram-sha-256" in pg["env"]["POSTGRES_INITDB_ARGS"]
            if "redis" in services:
                assert re.fullmatch(r"redis:7\.4(\.\d+)?@sha256:[0-9a-f]{64}", services["redis"]["image"])
            dumped = yaml.safe_dump(job)
            if "localhost:5432" in dumped:
                assert "postgres" in services, f"{wf.name}/{name}: localhost database without its own service"
            if "localhost:6379" in dumped:
                assert "redis" in services, f"{wf.name}/{name}: localhost Redis without its own service"
        for uses in re.findall(r"uses:\s*(\S+)", text):
            assert re.search(r"@[0-9a-f]{40}$", uses), f"{wf.name}: action not pinned by SHA: {uses}"


@pytest.mark.parametrize(
    ("text", "flagged"),
    [
        ("postgresql://atlas_app:${APP_PW}@postgres:5432/atlas_db", False),
        ("postgresql://atlas_app:change-me@localhost/atlas_db", False),
        ("postgresql://atlas_app:s3cretValue@localhost/atlas_db", True),
        ("postgresql://atlas_app:${APP_PW}x@localhost/atlas_db", True),
        ("redis://user:$APP_PW@host/11", True),
    ],
)
def test_password_in_url_rule(text: str, flagged: bool) -> None:
    assert bool(SECRET_PATTERNS["password in url"].search(text)) is flagged
