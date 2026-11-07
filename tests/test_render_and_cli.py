import base64
import contextlib
import io
import re
import tempfile
import unittest
import zlib
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest import mock

import chess
from helpers import REAL_ROWS, SYNTHETIC_ROWS, START, write_csv

import main
from pdf_renderer import CoverInfo, PuzzleError, _rating_label, humanize_theme, prepare_puzzle, render_pdf
from scripts.import_database import import_csv


def pdf_text(pdf: Path) -> bytes:
    """Decompressed content of all page streams (reportlab uses ASCII85 + Flate)."""
    data = pdf.read_bytes()
    out = []
    for stream in re.findall(rb"stream\r?\n(.*?)~>endstream", data, re.S):
        out.append(zlib.decompress(base64.a85decode(stream.strip())))
    return b"\n".join(out)


def page_count(pdf: Path) -> int:
    return len(re.findall(rb"/Type\s*/Page[^s]", pdf.read_bytes()))


class PrepareTests(unittest.TestCase):
    def row(self, fen, moves):
        return {"PuzzleId": "x", "FEN": fen, "Moves": moves, "Rating": 1500, "Themes": "fork"}

    def test_first_move_is_played_and_solution_is_san(self):
        p = prepare_puzzle(self.row("q3k1nr/1pp1nQpp/3p4/1P2p3/4P3/B1PP1b2/B5PP/5K2 b k - 0 17",
                                    "e8d7 a2e6 d7d8 f7f8"))
        self.assertEqual(p.solver, chess.WHITE)
        self.assertEqual(p.to_move, "White to move")
        self.assertEqual(p.last_move_san, "17...Kd7")
        self.assertEqual(p.solution_san, "18. Be6+ Kd8 19. Qf8#")

    def test_black_solver(self):
        p = prepare_puzzle(self.row("r3r1k1/p4ppp/2p2n2/1p6/3P1qb1/2NQR3/PPB2PP1/R1B3K1 w - - 5 18",
                                    "e3g3 e8e1 g1h2 e1c1 a1c1 f4h6 h2g1 h6c1"))
        self.assertEqual(p.to_move, "Black to move")
        self.assertTrue(p.solution_san.startswith("18...Re1+"))

    def test_illegal_move_raises(self):
        with self.assertRaises(PuzzleError):
            prepare_puzzle(self.row(START, "e2e5 e7e5"))
        with self.assertRaises(PuzzleError):
            prepare_puzzle(self.row(START, "e2e4"))


class RenderTests(unittest.TestCase):
    def test_double_sided_page_layout(self):
        rows = [dict(zip(["PuzzleId", "FEN", "Moves", "Rating"], r.split(",")[:4])) for r in REAL_ROWS]
        for r in rows:
            r["Rating"] = int(r["Rating"])
        puzzles = [prepare_puzzle(r) for r in rows]
        with tempfile.TemporaryDirectory() as tmp:
            # Even count: cover, blank, 4 puzzles, answer key (page 7 = front of a sheet).
            pdf = render_pdf(puzzles, Path(tmp) / "even.pdf")
            self.assertEqual(page_count(pdf), 1 + 1 + 4 + 1)
            self.assertIn(b"/MediaBox [ 0 0 419.5276 595.2756 ]", pdf.read_bytes())  # A5
            # Odd count: cover, blank, 3 puzzles, blank, answer key (page 7 again).
            pdf = render_pdf(puzzles[:3], Path(tmp) / "odd.pdf")
            self.assertEqual(page_count(pdf), 1 + 1 + 3 + 1 + 1)

    def test_long_answer_key_spans_pages(self):
        row = {"PuzzleId": "00sHx", "FEN": "q3k1nr/1pp1nQpp/3p4/1P2p3/4P3/B1PP1b2/B5PP/5K2 b k - 0 17",
               "Moves": "e8d7 a2e6 d7d8 f7f8", "Rating": 1760, "Themes": "mate"}
        puzzles = [prepare_puzzle(row)] * 60
        with tempfile.TemporaryDirectory() as tmp:
            pdf = render_pdf(puzzles, Path(tmp) / "out.pdf")
            self.assertEqual(page_count(pdf), 1 + 1 + 60 + 3)  # 60 solutions = 3 pages of 20

    def test_exactly_20_solutions_per_page(self):
        row = {"PuzzleId": "00sHx", "FEN": "q3k1nr/1pp1nQpp/3p4/1P2p3/4P3/B1PP1b2/B5PP/5K2 b k - 0 17",
               "Moves": "e8d7 a2e6 d7d8 f7f8", "Rating": 1760, "Themes": "mate"}
        p = prepare_puzzle(row)
        long_one = replace(p, solution_san=" ".join(["18. Be6+ Kd8 19. Qf8+ Kc7"] * 12))
        with tempfile.TemporaryDirectory() as tmp:
            for n, answer_pages in ((20, 1), (21, 2), (40, 2), (41, 3)):
                puzzles = [long_one if i % 7 == 0 else p for i in range(n)]
                pdf = render_pdf(puzzles, Path(tmp) / f"{n}.pdf")
                self.assertEqual(page_count(pdf), 1 + 1 + n + n % 2 + answer_pages, n)

    def test_cover_text(self):
        row = {"PuzzleId": "00sHx", "FEN": "q3k1nr/1pp1nQpp/3p4/1P2p3/4P3/B1PP1b2/B5PP/5K2 b k - 0 17",
               "Moves": "e8d7 a2e6 d7d8 f7f8", "Rating": 1760, "Themes": "mate"}
        cover = CoverInfo(puzzle_count=1, min_rating=1300, max_rating=1600,
                          themes=["fork", "discoveredAttack"], set_number=12, title="Summer set",
                          date=datetime(2026, 11, 7))
        with tempfile.TemporaryDirectory() as tmp:
            pdf = render_pdf([prepare_puzzle(row)], Path(tmp) / "out.pdf", cover=cover)
            data = pdf_text(pdf)
        for text in (b"SUMMER SET", b"Set #12", b"7 November 2026", b"Rating 1300", b"Discovered attack"):
            self.assertIn(text, data)

    def test_humanize_theme_and_rating_label(self):
        self.assertEqual(humanize_theme("discoveredAttack"), "Discovered attack")
        self.assertEqual(humanize_theme("mateIn2"), "Mate in 2")
        self.assertEqual(humanize_theme("fork"), "Fork")
        self.assertEqual(humanize_theme("xRayAttack"), "X-ray attack")
        self.assertEqual(_rating_label(0, 4000), "All ratings")
        self.assertEqual(_rating_label(1500, 4000), "Rating 1500+")
        self.assertEqual(_rating_label(0, 1200), "Rating up to 1200")


class CliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db = self.tmp / "p.db"
        self.hist = self.tmp / "h.db"
        valid_synthetic = [r for r in SYNTHETIC_ROWS]  # start position + e2e4 e7e5 is legal
        import_csv(write_csv(self.tmp / "p.csv", REAL_ROWS + valid_synthetic), self.db, progress=False)

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main.main([*args, "--db", str(self.db), "--history", str(self.hist)])
        return code, out.getvalue() + err.getvalue()

    def test_generate_records_history_and_stops_early(self):
        pdf = self.tmp / "a.pdf"
        code, out = self.run_cli("generate", "-n", "2", "--themes", "FORK", "-o", str(pdf))
        self.assertEqual(code, 0, out)
        self.assertEqual(page_count(pdf), 5)  # cover, blank, 2 puzzles, answer key
        self.assertIn("set #1", out)

        # 7 fork puzzles; 2 fail the default quality filter (00sJb: 64 plays,
        # TLOWQ), 2 already printed -> 3 left.
        pdf2 = self.tmp / "b.pdf"
        code, out = self.run_cli("generate", "-n", "100", "--themes", "fork", "-o", str(pdf2))
        self.assertEqual(code, 0, out)
        self.assertIn("Only 3 puzzle(s) match", out)
        self.assertEqual(page_count(pdf2), 7)  # cover, blank, 3 puzzles, blank, answer key

        code, out = self.run_cli("generate", "--themes", "fork", "-o", str(self.tmp / "c.pdf"))
        self.assertEqual(code, 1)
        self.assertIn("No puzzles match", out)

        code, out = self.run_cli("generate", "-n", "3", "--themes", "fork", "--allow-repeats",
                                 "-o", str(self.tmp / "d.pdf"))
        self.assertEqual(code, 0, out)

        code, out = self.run_cli("history")
        self.assertIn("#3", out)
        self.assertIn("repeats allowed", out)

    def test_failed_render_removes_run_from_history(self):
        with mock.patch("main.render_pdf", side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                self.run_cli("generate", "-n", "2", "-o", str(self.tmp / "x.pdf"))
        code, out = self.run_cli("history")
        self.assertIn("No runs recorded", out)

    def test_cleanup_without_options_only_lists(self):
        download = self.tmp / "lichess_db_puzzle.csv.zst"
        download.write_bytes(b"x" * 1000)
        code, out = self.run_cli("cleanup")
        self.assertEqual(code, 0)
        self.assertIn("--downloads", out)
        self.assertIn("--database", out)
        self.assertTrue(download.exists())
        self.assertTrue(self.db.exists())

    def test_cleanup_downloads_asks_and_keeps_database(self):
        download = self.tmp / "lichess_db_puzzle.csv.zst"
        download.write_bytes(b"x" * 1000)
        with mock.patch("builtins.input", return_value="n"):
            code, out = self.run_cli("cleanup", "--downloads")
        self.assertIn("Cancelled", out)
        self.assertTrue(download.exists())
        with mock.patch("builtins.input", return_value="y"):
            code, out = self.run_cli("cleanup", "--downloads")
        self.assertEqual(code, 0, out)
        self.assertFalse(download.exists())
        self.assertTrue(self.db.exists())

    def test_cleanup_database_keeps_history(self):
        self.run_cli("generate", "-n", "1", "-o", str(self.tmp / "a.pdf"))
        code, out = self.run_cli("cleanup", "--database", "--yes")
        self.assertEqual(code, 0, out)
        self.assertFalse(self.db.exists())
        self.assertTrue(self.hist.exists())
        code, out = self.run_cli("history")  # works without the puzzle database
        self.assertEqual(code, 0, out)
        self.assertIn("#1", out)
        code, out = self.run_cli("generate", "-n", "1")
        self.assertEqual(code, 1)
        self.assertIn("database.lichess.org", out)

    def test_unknown_theme(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli("generate", "--themes", "notATheme")
        self.assertIn("Unknown theme", str(ctx.exception))

    def test_themes_and_reset(self):
        code, out = self.run_cli("themes")
        self.assertIn("fork", out)
        self.run_cli("generate", "-n", "1", "-o", str(self.tmp / "a.pdf"))
        code, out = self.run_cli("reset-history", "--yes")
        self.assertEqual(code, 0)
        code, out = self.run_cli("history")
        self.assertIn("No runs recorded", out)


if __name__ == "__main__":
    unittest.main()
