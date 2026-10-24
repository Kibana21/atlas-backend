"""Read-only Redis evidence (ATLAS-001 B-15/B-17). Commands: DBSIZE, or bounded SCAN under ``atlas:``.

Usage:
  python scripts/redis_keys.py --db 11 --pattern 'atlas:test:*'   # count Atlas keys (SCAN, bounded)
  python scripts/redis_keys.py --db 1 --dbsize                    # key count only; names never read

Host and port come from ATLAS_REDIS_URL (settings / ATLAS_ENV_FILE). Patterns must start with
``atlas:``, so keys of other applications are never listed. Nothing is written or deleted.
"""

from __future__ import annotations

import argparse
import sys
from urllib.parse import urlsplit

import redis

from atlas.core.settings import get_settings

SCAN_CAP = 10_000


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=int, required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pattern")
    group.add_argument("--dbsize", action="store_true")
    args = parser.parse_args(argv)
    if args.pattern is not None and not args.pattern.startswith("atlas:"):
        print("REFUSED: patterns must start with 'atlas:'", file=sys.stderr)
        return 2
    url = urlsplit(get_settings().redis_url)
    client = redis.Redis(host=url.hostname or "localhost", port=url.port or 6379, db=args.db, socket_timeout=2)
    try:
        if args.dbsize:
            print(f"db {args.db}: dbsize {client.dbsize()}")
            return 0
        count = 0
        for _ in client.scan_iter(match=args.pattern, count=100):
            count += 1
            if count > SCAN_CAP:
                print(f"REFUSED: more than {SCAN_CAP} keys match", file=sys.stderr)
                return 2
        print(f"db {args.db}: {count} keys match {args.pattern!r}")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
