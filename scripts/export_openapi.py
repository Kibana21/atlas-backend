"""Write the application's OpenAPI document deterministically (FR-36). Usage: python scripts/export_openapi.py OUT.json

The schema is environment-invariant (FR-22), so a fixed test configuration is used; nothing is
read from .env and no service is contacted.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from atlas.core.settings import Settings
from atlas.main import create_app

EXPORT_SETTINGS = {
    "env": "test",
    "database_url": "postgresql+psycopg://atlas_test_app@localhost:5432/atlas_test",
    "migration_database_url": "postgresql+psycopg://atlas_test_migrator@localhost:5432/atlas_test",
    "db_app_role": "atlas_test_app",
    "redis_url": "redis://localhost:6379/11",
    "redis_key_prefix": "atlas:test:export:",
}


def export() -> str:
    app = create_app(Settings(_env_file=None, **EXPORT_SETTINGS))  # type: ignore[arg-type]
    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    Path(sys.argv[1]).write_text(export(), encoding="utf-8")
