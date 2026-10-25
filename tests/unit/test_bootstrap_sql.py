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


INGEST_COPIES = ("bootstrap-ingest.sql", "bootstrap-ingest-verify.sql", "bootstrap-ingest-rollback.sql")


@pytest.mark.parametrize("name", INGEST_COPIES)
def test_ingest_copies_have_a_ci_header_and_no_ci_block(name: str) -> None:
    text = (ROOT / "scripts" / "db" / name).read_text()
    body = strip_ci_additions(text, header_only=True)
    assert body.startswith("-- =====")
    assert "CI-ONLY" not in text
    assert not re.search(r"PASSWORD\s+'", text)  # passwords come from psql variables only


def test_ingest_setup_touches_only_the_two_ingestion_roles() -> None:
    text = strip_ci_additions((ROOT / "scripts" / "db" / "bootstrap-ingest.sql").read_text(), header_only=True)
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("--"))  # statements only
    created = re.findall(r"^CREATE ROLE (\w+)", body, flags=re.MULTILINE)
    altered = set(re.findall(r"^ALTER ROLE (\w+)", body, flags=re.MULTILINE))
    assert created == ["atlas_ingest", "atlas_test_ingest"]
    assert altered == {"atlas_ingest", "atlas_test_ingest"}
    assert not re.search(r"\b(SUPERUSER|CREATEDB|CREATEROLE|REPLICATION|BYPASSRLS|ALTER SYSTEM|DROP)\b", body)
    assert not re.search(r"\bGRANT\b.*\bON (TABLE|ALL TABLES)", body)  # table privileges come from migration 0002


def test_header_only_mode(tmp_path: Path) -> None:
    (tmp_path / "b.sql").write_text("-- ci\n-- END CI HEADER\na\n")
    (tmp_path / "p.sql").write_text("a\n")
    assert main([str(tmp_path / "b.sql"), str(tmp_path / "p.sql"), "--header-only"]) == 0
    (tmp_path / "b.sql").write_text("-- ci\n-- END CI HEADER\na\n-- BEGIN CI-ONLY\nx\n-- END CI-ONLY\n")
    assert main([str(tmp_path / "b.sql"), str(tmp_path / "p.sql"), "--header-only"]) == 1
