import tempfile
import unittest
from pathlib import Path

from helpers import REAL_ROWS, SYNTHETIC_ROWS, write_csv

from puzzle_database import PuzzleDatabase
from scripts.import_database import import_csv


class QueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmp.name)
        cls.db_path = tmp / "puzzles.db"
        import_csv(write_csv(tmp / "p.csv", REAL_ROWS + SYNTHETIC_ROWS), cls.db_path, progress=False)
        cls.history_path = tmp / "history.db"

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        self.db = PuzzleDatabase(self.db_path, self.history_path)
        self.db.reset_history()

    def tearDown(self):
        self.db.close()

    def ids(self, **kwargs):
        kwargs.setdefault("limit", 100)
        return sorted(p["PuzzleId"] for p in self.db.get_puzzles(**kwargs))

    def test_rating_range_is_inclusive(self):
        self.assertEqual(self.ids(min_rating=1300, max_rating=1600),
                         ["T1300", "T1450", "T1500", "T1600", "TLOWQ", "TNULL"])

    def test_any_theme(self):
        self.assertEqual(self.ids(min_rating=1300, max_rating=1600, themes=["skewer", "mate"]),
                         ["T1450", "T1600"])

    def test_all_themes(self):
        self.assertEqual(self.ids(themes=["fork", "skewer"], match_all_themes=True), ["T1450"])
        # Duplicate input must not change AND semantics.
        self.assertEqual(self.ids(themes=["fork", "skewer", "fork"], match_all_themes=True), ["T1450"])

    def test_exclude_themes(self):
        self.assertEqual(self.ids(min_rating=1300, max_rating=1600, themes=["fork"], exclude_themes=["mate"]),
                         ["T1300", "TLOWQ"])
        self.assertEqual(self.ids(min_rating=1300, max_rating=1600, exclude_themes=["fork", "skewer"]),
                         ["T1500", "TNULL"])

    def test_empty_theme_filter_means_no_filter(self):
        self.assertEqual(self.ids(themes=[]), self.ids(themes=None))
        self.assertEqual(len(self.ids()), 12)

    def test_theme_match_is_exact_not_substring(self):
        # "mate" must not match "mateIn2" or "smotheredMate" (T1500).
        self.assertEqual(self.ids(themes=["mate"]), ["00sHx", "T1450"])
        self.assertEqual(self.ids(themes=["Mate"]), [])

    def test_quality_filters(self):
        ids = self.ids(min_rating=1300, max_rating=1600, min_popularity=50, min_plays=100)
        self.assertNotIn("TLOWQ", ids)
        self.assertNotIn("TNULL", ids)  # NULL popularity does not pass a quality filter
        self.assertIn("T1300", ids)

    def test_limit_and_random(self):
        self.assertEqual(len(self.db.get_puzzles(limit=3, randomize=True)), 3)
        seen = set()
        for _ in range(30):
            seen.update(p["PuzzleId"] for p in self.db.get_puzzles(limit=1, randomize=True))
        self.assertGreater(len(seen), 3)

    def test_count_matches_get(self):
        crit = dict(min_rating=1300, max_rating=1600, themes=["fork"])
        self.assertEqual(self.db.count_puzzles(**crit), len(self.ids(**crit)))

    def test_results_contain_original_fields(self):
        p = self.db.get_puzzles(themes=["discoveredAttack"])[0]
        self.assertEqual(p["PuzzleId"], "00sO1")
        self.assertEqual(p["GameUrl"], "https://lichess.org/vsfFkG0s/black#62")
        self.assertEqual(p["Themes"], "advantage discoveredAttack master middlegame short")

    def test_invalid_arguments(self):
        bad = [
            dict(min_rating=1600, max_rating=1300),
            dict(min_rating=-1),
            dict(min_rating="1300"),
            dict(limit=0),
            dict(limit=-5),
            dict(limit=2.5),
            dict(limit=True),
            dict(themes="fork"),
            dict(themes=[""]),
            dict(themes=["fork skewer"]),
            dict(themes=["fork"], exclude_themes=["fork"]),
            dict(min_plays="many"),
        ]
        for kwargs in bad:
            with self.subTest(**kwargs), self.assertRaises(ValueError):
                self.db.get_puzzles(**kwargs)

    def test_values_are_parameters_not_sql(self):
        self.assertEqual(self.ids(themes=["fork')OR(1=1)--"]), [])

    # -- history -------------------------------------------------------------

    def test_exclude_seen(self):
        crit = dict(min_rating=1300, max_rating=1600, themes=["fork"])
        first = self.ids(**crit, exclude_seen=True)
        self.assertEqual(len(first), 3)
        self.db.record_run(first[:2], crit, "x.pdf")
        self.assertEqual(self.ids(**crit, exclude_seen=True), first[2:])
        self.assertEqual(self.ids(**crit, exclude_seen=False), first)  # repeats allowed
        self.assertEqual(self.db.seen_count(), 2)

    def test_history_runs_and_reset(self):
        run_id = self.db.record_run(["T1300", "T1450"], {"min_rating": 1300}, "a.pdf")
        runs = self.db.list_runs()
        self.assertEqual([r["RunId"] for r in runs], [run_id])
        self.assertEqual(runs[0]["Criteria"], {"min_rating": 1300})
        self.db.reset_history()
        self.assertEqual(self.db.list_runs(), [])
        self.assertEqual(self.db.seen_count(), 0)

    def test_history_survives_reimport(self):
        self.db.record_run(["T1300"], {}, "a.pdf")
        self.db.close()
        import_csv(write_csv(Path(self._tmp.name) / "p2.csv", REAL_ROWS + SYNTHETIC_ROWS),
                   self.db_path, keep_backup=False, progress=False)
        self.db = PuzzleDatabase(self.db_path, self.history_path)
        self.assertEqual(self.db.seen_count(), 1)
        self.assertNotIn("T1300", self.ids(exclude_seen=True))

    def test_theme_counts(self):
        counts = self.db.theme_counts()
        self.assertEqual(counts["fork"], 2 + 5)
        self.assertNotIn("", counts)

    def test_query_plan_uses_indexes(self):
        plan = " | ".join(self.db.explain(min_rating=1300, max_rating=1600, themes=["fork"]))
        self.assertIn("SEARCH puzzle_themes USING PRIMARY KEY (Theme=? AND Rating>? AND Rating<?)", plan)
        plan = " | ".join(self.db.explain(min_rating=1300, max_rating=1600, min_plays=10))
        self.assertIn("SEARCH puzzles USING COVERING INDEX idx_puzzles_rating", plan)
        self.assertNotIn("SCAN puzzles", plan)


class MissingDatabaseTest(unittest.TestCase):
    def test_missing_database_has_helpful_error(self):
        with self.assertRaises(FileNotFoundError) as ctx:
            PuzzleDatabase(Path(tempfile.gettempdir()) / "does_not_exist.db", None)
        self.assertIn("import_database.py", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
