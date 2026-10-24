"""Build provenance (ATLAS-001 FR-27, spec §6.5).

backend_sha: ATLAS_BUILD_SHA (CI/images) -> `git rev-parse HEAD` (local checkout) -> "unknown".
specs_sha / contract_version: the pinned specs.lock (DEC-039).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SPECS_LOCK = REPO_ROOT / "specs.lock"
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
UNPINNED_VERSION = "0.0.0-unpinned"


@dataclass(frozen=True)
class SpecsPin:
    specs_commit: str
    contract_version: str


def read_specs_lock(path: Path = SPECS_LOCK) -> SpecsPin | None:
    if not path.exists():
        return None
    fields: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition(":")
        if sep and not line.startswith((" ", "#")):
            fields[key.strip()] = value.strip().strip('"').strip("'")
    commit, version = fields.get("specs_commit", ""), fields.get("contract_version", "")
    if not FULL_SHA.fullmatch(commit) or not version:
        return None
    return SpecsPin(commit, version)


def contract_version() -> str:
    pin = read_specs_lock()
    return pin.contract_version if pin else UNPINNED_VERSION


@lru_cache(maxsize=4)
def backend_sha(build_sha: str | None) -> str:
    if build_sha and FULL_SHA.fullmatch(build_sha):
        return build_sha
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell, no user input
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],  # noqa: S607 - git resolved from PATH
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out if FULL_SHA.fullmatch(out) else "unknown"
