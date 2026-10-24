"""B-16 / FR-34: no source or test file contains server-wide or keyspace-wide Redis commands.

Forbidden: flush-all/flush-db, KEYS pattern listing, CONFIG SET, SWAPDB and SELECT (the client is
bound to one DB by its URL). This file is the only one allowed to spell the forbidden names.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.security]
ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN = {
    "FLUSHALL/FLUSHDB": re.compile(r"flush_?(all|db)", re.I),
    "SWAPDB": re.compile(r"swap_?db", re.I),
    "CONFIG SET": re.compile(r"config[\s_]*set", re.I),
    "KEYS pattern": re.compile(r"\.keys\(\s*[^\s)]|execute_command\(\s*['\"]keys\b", re.I),
    "SELECT db": re.compile(r"\.select\(\s*\d|execute_command\(\s*['\"]select\b", re.I),
}


def forbidden_redis_commands(root: Path, *, skip: Path | None = None) -> list[str]:
    hits = []
    for path in sorted(root.rglob("*.py")):
        if skip is not None and path.resolve() == skip.resolve():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        hits += [f"{path.relative_to(root)}: {name}" for name, rx in FORBIDDEN.items() if rx.search(text)]
    return hits


def test_src_and_tests_contain_no_forbidden_redis_commands() -> None:
    this = Path(__file__)
    assert forbidden_redis_commands(ROOT / "src") == []
    assert forbidden_redis_commands(ROOT / "tests", skip=this) == []
    assert forbidden_redis_commands(ROOT / "scripts") == []


@pytest.mark.parametrize(
    "snippet",
    [
        "await r.flushdb()",
        "client.execute_command('FLUSHALL')",
        "await r.config_set('maxmemory', 1)",
        "await r.swapdb(1, 2)",
        "for k in await r.keys('*'):",
        "r.execute_command('SELECT', 1)",
        "conn.select(1)",
    ],
)
def test_a_file_with_a_forbidden_command_fails_the_check(tmp_path: Path, snippet: str) -> None:
    (tmp_path / "bad.py").write_text(snippet + "\n")
    assert forbidden_redis_commands(tmp_path) != []


def test_ordinary_code_is_not_flagged(tmp_path: Path) -> None:
    (tmp_path / "ok.py").write_text("d = {}\nprint(d.keys())\nq = 'SELECT 1'\nawait r.delete_exact(k)\n")
    assert forbidden_redis_commands(tmp_path) == []
