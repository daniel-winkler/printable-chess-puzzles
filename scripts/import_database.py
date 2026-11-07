"""Import the Lichess puzzle CSV into a local SQLite database.

Usage:
    python scripts/import_database.py [CSV_PATH] [--db PATH] [--batch-size N] [--no-backup]

The CSV may be plain (.csv) or zstandard-compressed (.csv.zst, as downloaded
from https://database.lichess.org/#puzzles). If CSV_PATH is omitted, the first
existing file among DEFAULT_CSV_CANDIDATES is used.

The import is built into a staging database first. The production database is
only replaced after the staging database passes validation, so a failed import
never damages a working database.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sqlite3
import sys
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_DB_PATH = DATA_DIR / "chess_puzzles.db"
DOWNLOAD_NAMES = ["puzzles.csv", "lichess_db_puzzle.csv", "lichess_db_puzzle.csv.zst"]
DEFAULT_CSV_CANDIDATES = [DATA_DIR / name for name in DOWNLOAD_NAMES]
DEFAULT_BATCH_SIZE = 10_000

REQUIRED_COLUMNS = [
    "PuzzleId", "FEN", "Moves", "Rating", "RatingDeviation",
    "Popularity", "NbPlays", "Themes", "GameUrl", "OpeningTags",
]
# Present in recent Lichess exports only; stored as NULL when absent.
OPTIONAL_COLUMNS = ["DailyDate"]

SCHEMA = """
CREATE TABLE puzzles (
    PuzzleId        TEXT PRIMARY KEY,
    FEN             TEXT NOT NULL,
    Moves           TEXT NOT NULL,
    Rating          INTEGER NOT NULL,
    RatingDeviation INTEGER,
    Popularity      INTEGER,
    NbPlays         INTEGER,
    Themes          TEXT,
    GameUrl         TEXT,
    OpeningTags     TEXT,
    DailyDate       TEXT
);

-- One row per (puzzle, theme). Rating, Popularity and NbPlays are copied from
-- `puzzles` and the table is clustered by (Theme, Rating): a query such as
-- "fork puzzles rated 1300-1600 with popularity >= 80" then reads one
-- contiguous slice of this table and never has to visit `puzzles` per
-- candidate. On 6M puzzles this took such queries from 5-70 s to under 1 s.
CREATE TABLE puzzle_themes (
    Theme      TEXT NOT NULL,
    Rating     INTEGER NOT NULL,
    PuzzleId   TEXT NOT NULL REFERENCES puzzles(PuzzleId),
    Popularity INTEGER,
    NbPlays    INTEGER,
    PRIMARY KEY (Theme, Rating, PuzzleId)
) WITHOUT ROWID;

-- Distinct themes and how many puzzles carry each; lets the app list and
-- validate themes instantly instead of scanning 20M+ theme rows.
CREATE TABLE theme_counts (
    Theme       TEXT PRIMARY KEY,
    PuzzleCount INTEGER NOT NULL
);

CREATE TABLE import_info (
    Key   TEXT PRIMARY KEY,
    Value TEXT
);
"""

# Covering index: rating range + quality filter + PuzzleId without touching the table.
INDEXES = """
CREATE INDEX idx_puzzles_rating ON puzzles(Rating, Popularity, NbPlays, PuzzleId);
"""

# Theme rows are first collected in a temporary table in CSV order, then
# copied into puzzle_themes sorted by its primary key (much faster than
# inserting 30M rows into a B-tree in random order).
THEME_LOAD_TABLE = "CREATE TEMP TABLE theme_load (PuzzleId TEXT NOT NULL, Theme TEXT NOT NULL)"
FILL_PUZZLE_THEMES = """
INSERT INTO puzzle_themes (Theme, Rating, PuzzleId, Popularity, NbPlays)
SELECT t.Theme, p.Rating, t.PuzzleId, p.Popularity, p.NbPlays
FROM temp.theme_load AS t
JOIN puzzles AS p ON p.PuzzleId = t.PuzzleId
ORDER BY t.Theme, p.Rating, t.PuzzleId
"""

INSERT_PUZZLE = "INSERT INTO puzzles VALUES (?,?,?,?,?,?,?,?,?,?,?)"
INSERT_THEME = "INSERT INTO temp.theme_load (PuzzleId, Theme) VALUES (?, ?)"


class ImportFailed(Exception):
    """Raised when the CSV cannot be imported or the result fails validation."""


# --------------------------------------------------------------------------- #
# CSV reading
# --------------------------------------------------------------------------- #

@contextmanager
def open_csv_text(path: Path):
    """Open a .csv or .csv.zst file as a text stream, without loading it into RAM.

    Yields (text_stream, fraction_read) where fraction_read() returns how much
    of the file on disk has been consumed so far (0.0 - 1.0), for progress display.
    """
    total = max(path.stat().st_size, 1)
    with open(path, "rb") as raw:
        def fraction_read() -> float:
            return min(raw.tell() / total, 1.0)

        if path.suffix.lower() == ".zst":
            try:
                import zstandard
            except ImportError as exc:  # pragma: no cover - depends on environment
                raise ImportFailed(
                    "Reading .zst files requires the 'zstandard' package "
                    "(pip install -r requirements.txt), or decompress the file first."
                ) from exc
            source = zstandard.ZstdDecompressor().stream_reader(raw, closefd=False)
        else:
            source = raw
        text = io.TextIOWrapper(source, encoding="utf-8", newline="")
        try:
            yield text, fraction_read
        finally:
            text.close()


def _format_seconds(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


class Progress:
    """Prints progress on one updating line in a terminal, or plain lines otherwise."""

    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.started = time.perf_counter()
        self.interactive = enabled and sys.stdout.isatty()
        self._last_update = 0.0

    def elapsed(self) -> float:
        return time.perf_counter() - self.started

    def reading(self, fraction: float, puzzles: int, force: bool = False) -> None:
        if not self.enabled:
            return
        now = time.perf_counter()
        if not force and now - self._last_update < (0.5 if self.interactive else 15):
            return
        self._last_update = now
        elapsed = self.elapsed()
        eta = f", ~{_format_seconds(elapsed / fraction - elapsed)} left" if 0.02 < fraction < 1 else ""
        line = (f"  Step 1/3  Reading CSV: {fraction:6.1%}  "
                f"({puzzles:,} puzzles, {_format_seconds(elapsed)} elapsed{eta})")
        if self.interactive:
            print("\r" + line.ljust(90), end="", flush=True)
        else:
            print(line, flush=True)

    def step(self, message: str) -> None:
        if not self.enabled:
            return
        if self.interactive:
            print()  # finish the updating line
            self.interactive = False
        print(f"  {message}  [{_format_seconds(self.elapsed())} elapsed]", flush=True)


def parse_int(value: str, column: str, line: int, required: bool) -> int | None:
    value = value.strip()
    if value == "":
        if required:
            raise ImportFailed(f"Line {line}: required column {column} is empty")
        return None
    try:
        return int(value)
    except ValueError:
        raise ImportFailed(f"Line {line}: column {column} is not an integer: {value!r}") from None


def none_if_empty(value: str) -> str | None:
    return value if value != "" else None


def iter_rows(text_stream):
    """Yield (line_number, puzzle_tuple, theme_list) for each CSV data row."""
    reader = csv.reader(text_stream)
    try:
        header = next(reader)
    except StopIteration:
        raise ImportFailed("The CSV file is empty") from None

    header = [h.strip().lstrip("﻿") for h in header]
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise ImportFailed(f"CSV header is missing required columns: {', '.join(missing)}")
    idx = {name: header.index(name) for name in REQUIRED_COLUMNS + OPTIONAL_COLUMNS if name in header}
    n_cols = len(header)

    def get(row, name):
        i = idx.get(name)
        return row[i] if i is not None else ""

    for row in reader:
        line = reader.line_num
        if not row:
            continue  # tolerate blank lines (e.g. trailing newline)
        if len(row) != n_cols:
            raise ImportFailed(f"Line {line}: expected {n_cols} fields, found {len(row)}")

        puzzle_id = get(row, "PuzzleId").strip()
        fen = get(row, "FEN").strip()
        moves = get(row, "Moves").strip()
        for name, value in (("PuzzleId", puzzle_id), ("FEN", fen), ("Moves", moves)):
            if not value:
                raise ImportFailed(f"Line {line}: required column {name} is empty")

        themes_raw = get(row, "Themes")
        # dict.fromkeys de-duplicates while keeping order.
        themes = list(dict.fromkeys(themes_raw.split()))

        puzzle = (
            puzzle_id,
            fen,
            moves,
            parse_int(get(row, "Rating"), "Rating", line, required=True),
            parse_int(get(row, "RatingDeviation"), "RatingDeviation", line, required=False),
            parse_int(get(row, "Popularity"), "Popularity", line, required=False),
            parse_int(get(row, "NbPlays"), "NbPlays", line, required=False),
            none_if_empty(themes_raw.strip()),
            none_if_empty(get(row, "GameUrl").strip()),
            none_if_empty(get(row, "OpeningTags").strip()),
            none_if_empty(get(row, "DailyDate").strip()),
        )
        yield line, puzzle, themes


# --------------------------------------------------------------------------- #
# Import
# --------------------------------------------------------------------------- #

def _insert_batch(conn: sqlite3.Connection, puzzles: list, themes: list, lines: list) -> None:
    """Insert one batch; on a constraint error, pinpoint the offending CSV line."""
    conn.execute("SAVEPOINT batch")
    try:
        conn.executemany(INSERT_PUZZLE, puzzles)
        conn.executemany(INSERT_THEME, themes)
        conn.execute("RELEASE batch")
        return
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK TO batch")

    # Slow path, only reached on bad data: insert row by row to find the culprit.
    for line, puzzle in zip(lines, puzzles):
        try:
            conn.execute(INSERT_PUZZLE, puzzle)
        except sqlite3.IntegrityError as exc:
            conn.execute("ROLLBACK TO batch")
            conn.execute("RELEASE batch")
            if "UNIQUE" in str(exc) or "PRIMARY KEY" in str(exc):
                raise ImportFailed(f"Line {line}: duplicate PuzzleId {puzzle[0]!r}") from None
            raise ImportFailed(f"Line {line}: {exc}") from None
    conn.execute("ROLLBACK TO batch")
    conn.execute("RELEASE batch")
    raise ImportFailed("Integrity error while inserting themes (unexpected)")


def build_database(csv_path: Path, db_path: Path, batch_size: int = DEFAULT_BATCH_SIZE,
                   progress: bool = True) -> dict:
    """Build a fresh database at db_path from csv_path. Returns import statistics."""
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")

    prog = Progress(progress)
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        # Referential integrity is enforced by the JOIN in FILL_PUZZLE_THEMES
        # plus the row-count check in validate_database(), which is far
        # cheaper than per-row foreign-key checks on 30M rows.
        conn.execute("PRAGMA foreign_keys = OFF")
        # The staging file is disposable until validated, so durability
        # guarantees can be relaxed for a much faster bulk load.
        conn.execute("PRAGMA journal_mode = OFF")
        conn.execute("PRAGMA synchronous = OFF")
        conn.execute("PRAGMA cache_size = -200000")  # ~200 MB page cache
        conn.executescript(SCHEMA)
        conn.execute(THEME_LOAD_TABLE)
        conn.execute("PRAGMA analysis_limit = 1000")  # keep ANALYZE fast on big tables

        theme_counter: Counter[str] = Counter()
        n_puzzles = n_theme_rows = 0
        puzzles, themes, lines = [], [], []

        conn.execute("BEGIN")
        with open_csv_text(csv_path) as (text, fraction_read):
            prog.reading(0.0, 0, force=True)
            for line, puzzle, puzzle_themes in iter_rows(text):
                puzzles.append(puzzle)
                lines.append(line)
                for theme in puzzle_themes:
                    themes.append((puzzle[0], theme))
                theme_counter.update(puzzle_themes)

                if len(puzzles) >= batch_size:
                    _insert_batch(conn, puzzles, themes, lines)
                    n_puzzles += len(puzzles)
                    n_theme_rows += len(themes)
                    puzzles, themes, lines = [], [], []
                    prog.reading(fraction_read(), n_puzzles)
            if puzzles:
                _insert_batch(conn, puzzles, themes, lines)
                n_puzzles += len(puzzles)
                n_theme_rows += len(themes)
            prog.reading(1.0, n_puzzles, force=True)

        conn.executemany("INSERT INTO theme_counts VALUES (?, ?)", sorted(theme_counter.items()))
        conn.executemany("INSERT INTO import_info VALUES (?, ?)", [
            ("source_file", csv_path.name),
            ("imported_at", datetime.now(timezone.utc).isoformat(timespec="seconds")),
            ("puzzle_count", str(n_puzzles)),
        ])
        conn.execute("COMMIT")

        prog.step(f"Step 2/3  Building the theme table ({n_theme_rows:,} rows, no progress "
                  "shown, usually 1-3 minutes)...")
        conn.execute("BEGIN")
        conn.execute(FILL_PUZZLE_THEMES)
        conn.execute("DROP TABLE temp.theme_load")
        conn.execute("COMMIT")

        prog.step("Step 3/3  Creating the index and checking the result...")
        conn.executescript(INDEXES)
        conn.execute("ANALYZE")
        validate_database(conn, n_puzzles, n_theme_rows)
        stats = {
            "puzzles": n_puzzles,
            "theme_rows": n_theme_rows,
            "distinct_themes": len(theme_counter),
            "seconds": round(prog.elapsed(), 1),
        }
    finally:
        conn.close()
    return stats


def validate_database(conn: sqlite3.Connection, expected_puzzles: int, expected_theme_rows: int) -> None:
    if expected_puzzles == 0:
        raise ImportFailed("The CSV contained no puzzles")
    (actual,) = conn.execute("SELECT COUNT(*) FROM puzzles").fetchone()
    if actual != expected_puzzles:
        raise ImportFailed(f"Row count mismatch: parsed {expected_puzzles}, stored {actual}")
    # puzzle_themes is filled through an inner JOIN on puzzles, so a theme row
    # whose puzzle is missing would be dropped and show up as a count mismatch.
    (actual_themes,) = conn.execute("SELECT COUNT(*) FROM puzzle_themes").fetchone()
    if actual_themes != expected_theme_rows:
        raise ImportFailed(f"Theme row mismatch: parsed {expected_theme_rows}, stored {actual_themes} "
                           "(some theme rows did not match a puzzle)")


def staging_path_for(db_path: Path) -> Path:
    return db_path.with_name(db_path.stem + "_staging" + db_path.suffix)


def backup_path_for(db_path: Path) -> Path:
    return db_path.with_name(db_path.stem + "_backup" + db_path.suffix)


def import_csv(csv_path: Path, db_path: Path = DEFAULT_DB_PATH, batch_size: int = DEFAULT_BATCH_SIZE,
               keep_backup: bool = True, progress: bool = True) -> dict:
    """Import csv_path into a staging database, validate it, then swap it into db_path."""
    csv_path, db_path = Path(csv_path), Path(db_path)
    if not csv_path.is_file():
        raise ImportFailed(f"CSV file not found: {csv_path}")
    db_path.parent.mkdir(parents=True, exist_ok=True)

    staging = staging_path_for(db_path)
    if staging.exists():
        staging.unlink()  # leftover from an interrupted run

    try:
        stats = build_database(csv_path, staging, batch_size, progress)
    except BaseException:
        if staging.exists():
            staging.unlink()
        raise

    # Swap in the validated database. The old one is kept as a backup (one
    # generation) unless keep_backup is False.
    backup = backup_path_for(db_path)
    try:
        if db_path.exists() and keep_backup:
            os.replace(db_path, backup)
            try:
                os.replace(staging, db_path)
            except OSError:
                os.replace(backup, db_path)  # put the previous database back
                raise
        else:
            os.replace(staging, db_path)
    except PermissionError as exc:
        raise ImportFailed(
            f"Could not replace {db_path}: it is probably open in another program "
            f"(close it and run the import again). The new database was left at {staging}."
        ) from exc
    stats["database"] = str(db_path)
    stats["backup"] = str(backup) if backup.exists() and keep_backup else None
    return stats


def find_default_csv() -> Path | None:
    return next((p for p in DEFAULT_CSV_CANDIDATES if p.is_file()), None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import the Lichess puzzle CSV into SQLite.")
    parser.add_argument("csv", nargs="?", type=Path,
                        help="Path to the .csv or .csv.zst file (default: look in data/)")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH,
                        help=f"Output database (default: {DEFAULT_DB_PATH.relative_to(PROJECT_ROOT)})")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--no-backup", action="store_true",
                        help="Do not keep the previous database as *_backup.db")
    args = parser.parse_args(argv)

    csv_path = args.csv or find_default_csv()
    if csv_path is None:
        names = ", ".join(p.name for p in DEFAULT_CSV_CANDIDATES)
        print(f"No CSV given and none found in data/ (looked for {names}).", file=sys.stderr)
        return 1

    print(f"Importing {csv_path} -> {args.db}")
    try:
        stats = import_csv(csv_path, args.db, args.batch_size, keep_backup=not args.no_backup)
    except ImportFailed as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        print("The existing database (if any) was left unchanged.", file=sys.stderr)
        return 1

    print("Import complete:")
    print(f"  Puzzles:             {stats['puzzles']:,}")
    print(f"  Theme relationships: {stats['theme_rows']:,}")
    print(f"  Distinct themes:     {stats['distinct_themes']}")
    print(f"  Time:                {stats['seconds']} s")
    if stats["backup"]:
        print(f"  Previous database kept at {stats['backup']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
