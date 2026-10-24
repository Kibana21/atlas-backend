"""scripts/db/bootstrap.sql is the approved proposal plus one documented CI-only block (B-22)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from scripts.db.check_bootstrap import main, strip_ci_additions

pytestmark = [pytest.mark.unit, pytest.mark.security]
ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = ROOT / "scripts" / "db" / "bootstrap.sql"


def test_the_ci_only_block_creates_atlas_db_and_nothing_else() -> None:
    text = BOOTSTRAP.read_text()
    block = text[text.index("-- BEGIN CI-ONLY") : text.index("-- END CI-ONLY")]
    statements = [line for line in block.splitlines() if line and not line.startswith("--")]
    assert statements == ["CREATE DATABASE atlas_db TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C';"]


def test_no_password_literals_in_bootstrap() -> None:
    assert not re.search(r"PASSWORD\s+'", BOOTSTRAP.read_text())  # passwords come from psql variables only


def test_strip_and_compare(tmp_path: Path) -> None:
    proposal = "a\nb\n"
    bootstrap = "-- ci\n-- END CI HEADER\na\n-- BEGIN CI-ONLY\nx\n-- END CI-ONLY\nb\n"
    assert strip_ci_additions(bootstrap) == proposal
    (tmp_path / "b.sql").write_text(bootstrap)
    (tmp_path / "p.sql").write_text(proposal)
    assert main([str(tmp_path / "b.sql"), str(tmp_path / "p.sql")]) == 0
    (tmp_path / "p.sql").write_text("a\nchanged\n")
    assert main([str(tmp_path / "b.sql"), str(tmp_path / "p.sql")]) == 1
    (tmp_path / "b.sql").write_text("a\nb\n")
    assert main([str(tmp_path / "b.sql"), str(tmp_path / "p.sql")]) == 1
