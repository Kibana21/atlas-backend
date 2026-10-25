"""Prove a CI SQL copy = the approved atlas-specs proposal + its documented CI additions (B-22; ATLAS-002 DP-3).

Usage: python scripts/db/check_bootstrap.py BOOTSTRAP_SQL PROPOSAL_SQL [--header-only]

``bootstrap.sql`` has a CI header and exactly one CI-ONLY block. The ingestion-role copies (``bootstrap-ingest*.sql``)
have the CI header only (``--header-only``): everything after it is the approved file, byte for byte.
"""

from __future__ import annotations

import difflib
import sys
from pathlib import Path

HEADER_END = "-- END CI HEADER"
BLOCK_START = "-- BEGIN CI-ONLY"
BLOCK_END = "-- END CI-ONLY"


def strip_ci_additions(text: str, *, header_only: bool = False) -> str:
    lines = text.splitlines(keepends=True)
    ends = [i for i, line in enumerate(lines) if line.startswith(HEADER_END)]
    starts = [i for i, line in enumerate(lines) if line.startswith(BLOCK_START)]
    stops = [i for i, line in enumerate(lines) if line.startswith(BLOCK_END)]
    if header_only:
        if len(ends) != 1 or starts or stops:
            raise ValueError("expected exactly one CI header and no CI-ONLY block")
        return "".join(lines[ends[0] + 1 :])
    if len(ends) != 1 or len(starts) != 1 or len(stops) != 1 or not ends[0] < starts[0] < stops[0]:
        raise ValueError("expected exactly one CI header and one CI-ONLY block, in that order")
    return "".join(lines[ends[0] + 1 : starts[0]] + lines[stops[0] + 1 :])


def main(argv: list[str]) -> int:
    header_only = "--header-only" in argv
    paths = [a for a in argv if a != "--header-only"]
    bootstrap, proposal = (Path(p).read_text(encoding="utf-8") for p in paths)
    try:
        remainder = strip_ci_additions(bootstrap, header_only=header_only)
    except ValueError as exc:
        print(f"BOOTSTRAP_INVALID: {exc}", file=sys.stderr)
        return 1
    if remainder != proposal:
        diff = difflib.unified_diff(proposal.splitlines(), remainder.splitlines(), "proposal", "bootstrap", lineterm="")
        print("BOOTSTRAP_DRIFT: differs from the approved proposal beyond the CI-ONLY block", file=sys.stderr)
        print("\n".join(diff), file=sys.stderr)
        return 1
    print(
        f"OK: {Path(paths[0]).name} = approved proposal + documented CI " + ("header" if header_only else "additions")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
