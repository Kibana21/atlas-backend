"""Worker entry point: ``taskiq worker atlas.workers.entrypoint:broker`` (``make worker``).

The only module that builds a broker at import time, because the taskiq CLI needs a module-level
object. Import reads settings (``ATLAS_*`` / ``ATLAS_ENV_FILE``) and builds the broker; it opens no
connection and runs no task. Nothing else in Atlas imports this module.
"""

from __future__ import annotations

from atlas.core.settings import get_settings
from atlas.workers.broker import build_broker

broker, tasks = build_broker(get_settings())
