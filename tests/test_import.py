import sqlite3
import tempfile
import unittest
from pathlib import Path

from helpers import HEADER, REAL_ROWS, SYNTHETIC_ROWS, START, write_csv

from scripts.import_database import ImportFailed, import_csv, staging_path_for


class ImportTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db = self.tmp / "puzzles.db"

    def tearDown(self):
        self._tmp.cleanup()

    def query(self, sql, params=()):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def test_imports_all_columns(self):
        csv = write_csv(self.tmp / "p.csv", REAL_ROWS)
        stats = import_csv(csv, self.db, progress=False)
        self.assertEqual(stats["puzzles"], 4)
        row = self.query("SELECT * FROM puzzles WHERE PuzzleId = '00sJ9'")[0]
        self.assertEqual(row, (
            "00sJ9", "r3r1k1/p4ppp/2p2n2/1p6/3P1qb1/2NQR3/PPB2PP1/R1B3K1 w - - 5 18",
            "e3g3 e8e1 g1h2 e1c1 a1c1 f4h6 h2g1 h6c1", 2671, 105, 87, 325,
            "advantage attraction fork middlegame sacrifice veryLong",
            "https://lichess.org/gyFeQsOE#35", "French_Defense French_Defense_Exchange_Variation",
            "1607774862751",
        ))

    def test_empty_optional_values_become_null(self):
        csv = write_csv(self.tmp / "p.csv", SYNTHETIC_ROWS)
        import_csv(csv, self.db, progress=False)
        row = self.query("SELECT RatingDeviation, Popularity, NbPlays, Themes, GameUrl, OpeningTags, DailyDate "
                         "FROM puzzles WHERE PuzzleId = 'TNULL'")[0]
        self.assertEqual(row, (None,) * 7)

    def test_themes_split_into_exact_tokens(self):
        csv = write_csv(self.tmp / "p.csv", REAL_ROWS)
        stats = import_csv(csv, self.db, progress=False)
        themes = [t for (t,) in self.query("SELECT Theme FROM puzzle_themes WHERE PuzzleId='00sHx' ORDER BY Theme")]
        self.assertEqual(themes, ["mate", "mateIn2", "middlegame", "short"])
        self.assertEqual(stats["theme_rows"], 4 + 6 + 3 + 5)
        counts = dict(self.query("SELECT Theme, PuzzleCount FROM theme_counts"))
        self.assertEqual(counts["fork"], 2)
        self.assertEqual(counts["advantage"], 3)

    def test_missing_daily_date_column_is_allowed(self):
        header = HEADER.rsplit(",", 1)[0]
        rows = [r.rsplit(",", 1)[0] for r in SYNTHETIC_ROWS]
        import_csv(write_csv(self.tmp / "p.csv", rows, header), self.db, progress=False)
        self.assertEqual(self.query("SELECT COUNT(*) FROM puzzles")[0][0], len(rows))

    def test_small_batches(self):
        csv = write_csv(self.tmp / "p.csv", REAL_ROWS + SYNTHETIC_ROWS)
        stats = import_csv(csv, self.db, batch_size=3, progress=False)
        self.assertEqual(stats["puzzles"], 12)

    def test_zstd_input(self):
        import zstandard
        csv = write_csv(self.tmp / "p.csv", REAL_ROWS)
        zst = self.tmp / "p.csv.zst"
        zst.write_bytes(zstandard.ZstdCompressor().compress(csv.read_bytes()))
        self.assertEqual(import_csv(zst, self.db, progress=False)["puzzles"], 4)

    # -- failures ------------------------------------------------------------

    def assert_import_fails(self, rows, message, header=HEADER, **kwargs):
        csv = write_csv(self.tmp / "bad.csv", rows, header)
        with self.assertRaises(ImportFailed) as ctx:
            import_csv(csv, self.db, progress=False, **kwargs)
        self.assertIn(message, str(ctx.exception))
        self.assertFalse(staging_path_for(self.db).exists(), "staging file should be cleaned up")

    def test_duplicate_id_fails_with_line_number(self):
        self.assert_import_fails(REAL_ROWS + [REAL_ROWS[1]], "Line 6: duplicate PuzzleId '00sJ9'", batch_size=2)

    def test_duplicate_within_one_batch(self):
        self.assert_import_fails([REAL_ROWS[0], REAL_ROWS[0]], "duplicate PuzzleId")

    def test_non_integer_rating_fails(self):
        self.assert_import_fails([f"X1,{START},e2e4 e7e5,abc,1,1,1,fork,,,"], "Line 2: column Rating")

    def test_empty_rating_fails(self):
        self.assert_import_fails([f"X1,{START},e2e4 e7e5,,1,1,1,fork,,,"], "Rating is empty")

    def test_bad_optional_integer_fails(self):
        self.assert_import_fails([f"X1,{START},e2e4 e7e5,1500,1,high,1,fork,,,"], "column Popularity")

    def test_empty_required_text_fails(self):
        self.assert_import_fails([f"X1,{START},,1500,1,1,1,fork,,,"], "Moves is empty")
        self.assert_import_fails([f",{START},e2e4,1500,1,1,1,fork,,,"], "PuzzleId is empty")

    def test_missing_column_fails(self):
        self.assert_import_fails(["a,b"], "missing required columns: Moves", header="PuzzleId,FEN")

    def test_wrong_field_count_fails(self):
        self.assert_import_fails([f"X1,{START},e2e4,1500"], "Line 2: expected 11 fields")

    def test_empty_csv_fails(self):
        self.assert_import_fails([], "no puzzles")

    def test_missing_file_fails(self):
        with self.assertRaises(ImportFailed):
            import_csv(self.tmp / "nope.csv", self.db, progress=False)

    # -- replacement ---------------------------------------------------------

    def test_failed_import_keeps_existing_database(self):
        import_csv(write_csv(self.tmp / "good.csv", REAL_ROWS), self.db, progress=False)
        before = self.db.read_bytes()
        self.assert_import_fails(SYNTHETIC_ROWS + [SYNTHETIC_ROWS[0]], "duplicate")
        self.assertEqual(self.db.read_bytes(), before)

    def test_successful_import_replaces_database_and_keeps_backup(self):
        import_csv(write_csv(self.tmp / "a.csv", REAL_ROWS), self.db, progress=False)
        stats = import_csv(write_csv(self.tmp / "b.csv", SYNTHETIC_ROWS), self.db, progress=False)
        self.assertEqual(self.query("SELECT COUNT(*) FROM puzzles")[0][0], len(SYNTHETIC_ROWS))
        backup = sqlite3.connect(stats["backup"])
        self.assertEqual(backup.execute("SELECT COUNT(*) FROM puzzles").fetchone()[0], 4)
        backup.close()

    def test_no_backup_option(self):
        import_csv(write_csv(self.tmp / "a.csv", REAL_ROWS), self.db, progress=False)
        stats = import_csv(write_csv(self.tmp / "b.csv", SYNTHETIC_ROWS), self.db,
                           keep_backup=False, progress=False)
        self.assertIsNone(stats["backup"])
        self.assertEqual(self.query("SELECT COUNT(*) FROM puzzles")[0][0], len(SYNTHETIC_ROWS))


if __name__ == "__main__":
    unittest.main()
