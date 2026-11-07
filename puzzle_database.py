"""Query interface for the puzzle database, plus the selection history.

The puzzle database (data/chess_puzzles.db) is rebuilt from scratch on every
import, so the history of which puzzles were already printed lives in a
separate file (data/history.db) that survives re-imports.
"""

from __future__ import annotations

import json
import random
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "chess_puzzles.db"
DEFAULT_HISTORY_PATH = PROJECT_ROOT / "data" / "history.db"

HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS hist.runs (
    RunId       INTEGER PRIMARY KEY AUTOINCREMENT,
    CreatedAt   TEXT NOT NULL,
    Criteria    TEXT NOT NULL,
    OutputPath  TEXT,
    PuzzleCount INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS hist.selections (
    RunId    INTEGER NOT NULL REFERENCES runs(RunId) ON DELETE CASCADE,
    Position INTEGER NOT NULL,
    PuzzleId TEXT NOT NULL,
    PRIMARY KEY (RunId, Position)
);
CREATE INDEX IF NOT EXISTS hist.idx_selections_puzzle ON selections(PuzzleId);
"""


class PuzzleDatabase:
    """Read puzzles from the SQLite database built by scripts/import_database.py.

    Use as a context manager so the connection is always closed:

        with PuzzleDatabase() as db:
            puzzles = db.get_puzzles(1300, 1600, themes=["fork"], limit=20)
    """

    def __init__(self, db_path: str | Path | None = DEFAULT_DB_PATH,
                 history_path: str | Path | None = DEFAULT_HISTORY_PATH):
        """db_path=None opens only the history (for history commands when the
        puzzle database has been removed); puzzle queries are then unavailable."""
        if db_path is None:
            if history_path is None:
                raise ValueError("Need a puzzle database, a history database, or both")
            self.conn = sqlite3.connect(":memory:")
        else:
            db_path = Path(db_path)
            if not db_path.is_file():
                raise FileNotFoundError(
                    f"Puzzle database not found at {db_path}.\n"
                    "Download lichess_db_puzzle.csv.zst from https://database.lichess.org/#puzzles "
                    "into the data/ folder, then run: python scripts/import_database.py"
                )
            self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.has_history = history_path is not None
        if self.has_history:
            Path(history_path).parent.mkdir(parents=True, exist_ok=True)
            self.conn.execute("ATTACH DATABASE ? AS hist", (str(history_path),))
            self.conn.executescript(HISTORY_SCHEMA)

    # -- context manager ---------------------------------------------------- #

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "PuzzleDatabase":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- puzzles ------------------------------------------------------------ #

    def get_puzzles(
        self,
        min_rating: int = 0,
        max_rating: int = 4000,
        themes: Iterable[str] | None = None,
        match_all_themes: bool = False,
        exclude_themes: Iterable[str] | None = None,
        limit: int = 20,
        randomize: bool = False,
        min_popularity: int | None = None,
        min_plays: int | None = None,
        exclude_seen: bool = False,
    ) -> list[dict]:
        """Return up to `limit` puzzles matching every given criterion.

        - Rating bounds are inclusive.
        - `themes`: a puzzle must have ANY of them, or ALL of them when
          `match_all_themes` is True. None/empty means no theme filter.
        - `exclude_themes`: puzzles with any of these themes are dropped.
        - `min_popularity` / `min_plays`: optional quality filters.
        - `exclude_seen`: skip puzzles recorded in the selection history.
        - `randomize`: return a uniformly random sample of the matches, in
          random order; otherwise matches come back ordered by PuzzleId.
        """
        _validate_limit(limit)
        sql, params = self._select_sql(
            (min_rating, max_rating, themes, match_all_themes, exclude_themes,
             min_popularity, min_plays, exclude_seen), limit, randomize)
        rows = [dict(row) for row in self.conn.execute(sql, params)]
        if randomize:
            random.shuffle(rows)  # IN (...) returns rows in PuzzleId order
        return rows

    def _select_sql(self, criteria: tuple, limit: int, randomize: bool) -> tuple[str, list]:
        # Step 1 picks PuzzleIds using indexes only; step 2 fetches the full
        # rows for just those IDs. ORDER BY RANDOM() over all matching IDs is a
        # uniform random sample.
        candidates, params = self._build_query(*criteria)
        order = "RANDOM()" if randomize else "PuzzleId"
        sql = (f"SELECT p.* FROM puzzles p WHERE p.PuzzleId IN "
               f"({candidates} ORDER BY {order} LIMIT ?) ORDER BY p.PuzzleId")
        return sql, params + [limit]

    def count_puzzles(self, min_rating: int = 0, max_rating: int = 4000, themes=None,
                      match_all_themes: bool = False, exclude_themes=None,
                      min_popularity: int | None = None, min_plays: int | None = None,
                      exclude_seen: bool = False) -> int:
        """Count the puzzles matching the same criteria as get_puzzles()."""
        sql, params = self._build_query(
            min_rating, max_rating, themes, match_all_themes, exclude_themes,
            min_popularity, min_plays, exclude_seen,
        )
        return self.conn.execute(f"SELECT COUNT(*) FROM ({sql})", params).fetchone()[0]

    def explain(self, **criteria) -> list[str]:
        """Return SQLite's query plan for get_puzzles(**criteria) (for benchmarking)."""
        randomize = criteria.pop("randomize", False)
        limit = criteria.pop("limit", 20)
        kw = _query_kwargs(criteria)
        order = ["min_rating", "max_rating", "themes", "match_all_themes", "exclude_themes",
                 "min_popularity", "min_plays", "exclude_seen"]
        defaults = [0, 4000, None, False, None, None, None, False]
        args = tuple(kw.get(k, d) for k, d in zip(order, defaults))
        sql, params = self._select_sql(args, limit, randomize)
        return [row["detail"] for row in self.conn.execute("EXPLAIN QUERY PLAN " + sql, params)]

    def _build_query(self, min_rating=0, max_rating=4000, themes=None, match_all_themes=False,
                     exclude_themes=None, min_popularity=None,
                     min_plays=None, exclude_seen=False) -> tuple[str, list]:
        """Return (sql, params) for a SELECT producing the matching PuzzleIds."""
        _validate_rating(min_rating, "min_rating")
        _validate_rating(max_rating, "max_rating")
        if min_rating > max_rating:
            raise ValueError(f"min_rating ({min_rating}) is greater than max_rating ({max_rating})")
        include = _clean_themes(themes, "themes")
        exclude = _clean_themes(exclude_themes, "exclude_themes")
        overlap = set(include) & set(exclude)
        if overlap:
            raise ValueError(f"Themes both included and excluded: {', '.join(sorted(overlap))}")
        for name, value in (("min_popularity", min_popularity), ("min_plays", min_plays)):
            if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
                raise ValueError(f"{name} must be an integer or None")
        if exclude_seen and not self.has_history:
            raise ValueError("exclude_seen requires a history database")

        # Only placeholders are generated dynamically; all values are parameters.
        # Rating, Popularity and NbPlays exist in both `puzzles` and
        # `puzzle_themes`, so the same conditions work on either table.
        where = ["Rating BETWEEN ? AND ?"]
        params: list = [min_rating, max_rating]
        if min_popularity is not None:
            where.append("Popularity >= ?")
            params.append(min_popularity)
        if min_plays is not None:
            where.append("NbPlays >= ?")
            params.append(min_plays)
        if exclude:
            # Non-correlated subquery: SQLite builds it once as a lookup table.
            marks = ",".join("?" * len(exclude))
            where.append(f"PuzzleId NOT IN (SELECT PuzzleId FROM puzzle_themes "
                         f"WHERE Theme IN ({marks}) AND Rating BETWEEN ? AND ?)")
            params += exclude + [min_rating, max_rating]
        if exclude_seen:
            where.append("PuzzleId NOT IN (SELECT PuzzleId FROM hist.selections)")

        if not include:
            return "SELECT PuzzleId FROM puzzles WHERE " + " AND ".join(where), params

        marks = ",".join("?" * len(include))
        where.insert(0, f"Theme IN ({marks})")
        params = include + params
        sql = "SELECT PuzzleId FROM puzzle_themes WHERE " + " AND ".join(where)
        if len(include) == 1:
            return sql, params
        if match_all_themes:
            # (Theme, PuzzleId) is unique, so COUNT(*) counts distinct matched themes.
            return sql + " GROUP BY PuzzleId HAVING COUNT(*) = ?", params + [len(include)]
        return sql + " GROUP BY PuzzleId", params

    def theme_counts(self) -> dict[str, int]:
        """All themes in the database, mapped to how many puzzles have them."""
        rows = self.conn.execute("SELECT Theme, PuzzleCount FROM theme_counts ORDER BY Theme")
        return {row["Theme"]: row["PuzzleCount"] for row in rows}

    def import_info(self) -> dict[str, str]:
        return {row["Key"]: row["Value"] for row in self.conn.execute("SELECT Key, Value FROM import_info")}

    # -- history ------------------------------------------------------------ #

    def record_run(self, puzzle_ids: list[str], criteria: dict, output_path: str | None) -> int:
        """Store a run and the puzzles it selected; returns the new RunId."""
        self._require_history()
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO hist.runs (CreatedAt, Criteria, OutputPath, PuzzleCount) VALUES (?, ?, ?, ?)",
                (datetime.now().isoformat(timespec="seconds"), json.dumps(criteria),
                 output_path, len(puzzle_ids)),
            )
            run_id = cur.lastrowid
            self.conn.executemany(
                "INSERT INTO hist.selections (RunId, Position, PuzzleId) VALUES (?, ?, ?)",
                [(run_id, i, pid) for i, pid in enumerate(puzzle_ids, start=1)],
            )
        return run_id

    def delete_run(self, run_id: int) -> None:
        """Remove one run and its selections (those puzzles become 'unseen' again)."""
        self._require_history()
        with self.conn:
            self.conn.execute("DELETE FROM hist.selections WHERE RunId = ?", (run_id,))
            self.conn.execute("DELETE FROM hist.runs WHERE RunId = ?", (run_id,))

    def list_runs(self) -> list[dict]:
        self._require_history()
        rows = self.conn.execute("SELECT * FROM hist.runs ORDER BY RunId")
        return [dict(r) | {"Criteria": json.loads(r["Criteria"])} for r in rows]

    def seen_count(self) -> int:
        self._require_history()
        return self.conn.execute("SELECT COUNT(DISTINCT PuzzleId) FROM hist.selections").fetchone()[0]

    def reset_history(self) -> None:
        self._require_history()
        with self.conn:
            self.conn.execute("DELETE FROM hist.selections")
            self.conn.execute("DELETE FROM hist.runs")

    def _require_history(self) -> None:
        if not self.has_history:
            raise ValueError("This PuzzleDatabase was opened without a history database")


# --------------------------------------------------------------------------- #
# validation helpers
# --------------------------------------------------------------------------- #

def _query_kwargs(criteria: dict) -> dict:
    allowed = {"min_rating", "max_rating", "themes", "match_all_themes", "exclude_themes",
               "min_popularity", "min_plays", "exclude_seen"}
    unknown = set(criteria) - allowed
    if unknown:
        raise TypeError(f"Unknown criteria: {', '.join(sorted(unknown))}")
    return criteria


def _validate_rating(value, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer, got {value!r}")


def _validate_limit(limit) -> None:
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError(f"limit must be a positive integer, got {limit!r}")


def _clean_themes(themes, name: str) -> list[str]:
    if themes is None:
        return []
    if isinstance(themes, str):
        raise ValueError(f"{name} must be a list of theme names, not a single string")
    cleaned = []
    for theme in themes:
        if not isinstance(theme, str) or not theme.strip() or any(c.isspace() for c in theme.strip()):
            raise ValueError(f"Invalid theme in {name}: {theme!r}")
        cleaned.append(theme.strip())
    return list(dict.fromkeys(cleaned))  # de-duplicate, keep order
