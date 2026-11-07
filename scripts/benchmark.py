"""Time representative queries and print their SQLite query plans.

Usage:
    python scripts/benchmark.py [--db PATH]

Uses a throwaway history database, so your real history is not touched.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from puzzle_database import DEFAULT_DB_PATH, PuzzleDatabase  # noqa: E402

QUALITY = dict(min_popularity=80, min_plays=100)
CASES = [
    ("Rating 1300-1600, no themes", dict(min_rating=1300, max_rating=1600)),
    ("Rating 1300-1600 + quality filter", dict(min_rating=1300, max_rating=1600, **QUALITY)),
    ("Whole database (0-4000) + quality filter", dict(min_rating=0, max_rating=4000, **QUALITY)),
    ("1300-1600, any of fork/skewer", dict(min_rating=1300, max_rating=1600, themes=["fork", "skewer"], **QUALITY)),
    ("1300-1600, mate AND sacrifice", dict(min_rating=1300, max_rating=1600, themes=["mate", "sacrifice"],
                                           match_all_themes=True, **QUALITY)),
    ("1300-1600, fork excluding mate", dict(min_rating=1300, max_rating=1600, themes=["fork"],
                                            exclude_themes=["mate"], **QUALITY)),
    ("1300-1600, rare theme (smotheredMate)", dict(min_rating=1300, max_rating=1600, themes=["smotheredMate"],
                                                   **QUALITY)),
    ("1300-1600, fork, excluding seen", dict(min_rating=1300, max_rating=1600, themes=["fork"],
                                             exclude_seen=True, **QUALITY)),
]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory() as tmp, PuzzleDatabase(args.db, Path(tmp) / "h.db") as db:
        print(f"Database: {args.db} ({db.import_info().get('puzzle_count', '?')} puzzles)\n")
        # Seed the throwaway history with 10,000 "printed" puzzles.
        seen = [p["PuzzleId"] for p in db.get_puzzles(min_rating=1300, max_rating=1600, limit=10_000,
                                                      randomize=True)]
        db.record_run(seen, {}, None)

        for name, criteria in CASES:
            t0 = time.perf_counter()
            count = db.count_puzzles(**criteria)
            t1 = time.perf_counter()
            rows = db.get_puzzles(**criteria, limit=args.limit, randomize=True)
            t2 = time.perf_counter()
            print(f"{name}")
            print(f"  matches: {count:>10,}   count: {t1 - t0:6.2f}s   random {args.limit}: {t2 - t1:6.2f}s"
                  f"   (got {len(rows)})")
            for step in db.explain(**criteria, randomize=True, limit=args.limit):
                print(f"    plan: {step}")
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
