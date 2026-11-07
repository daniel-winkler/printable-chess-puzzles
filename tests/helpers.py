"""Shared fixtures: small CSV files written to a temporary directory."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

HEADER = "PuzzleId,FEN,Moves,Rating,RatingDeviation,Popularity,NbPlays,Themes,GameUrl,OpeningTags,DailyDate"

# Real Lichess puzzles (valid FEN + moves, so they can also be rendered).
REAL_ROWS = [
    "00sHx,q3k1nr/1pp1nQpp/3p4/1P2p3/4P3/B1PP1b2/B5PP/5K2 b k - 0 17,e8d7 a2e6 d7d8 f7f8,1760,80,83,72,"
    "mate mateIn2 middlegame short,https://lichess.org/yyznGmXs/black#34,Italian_Game Italian_Game_Classical_Variation,",
    "00sJ9,r3r1k1/p4ppp/2p2n2/1p6/3P1qb1/2NQR3/PPB2PP1/R1B3K1 w - - 5 18,e3g3 e8e1 g1h2 e1c1 a1c1 f4h6 h2g1 h6c1,"
    "2671,105,87,325,advantage attraction fork middlegame sacrifice veryLong,https://lichess.org/gyFeQsOE#35,"
    "French_Defense French_Defense_Exchange_Variation,1607774862751",
    "00sJb,Q1b2r1k/p2np2p/5bp1/q7/5P2/4B3/PPP3PP/2KR1B1R w - - 1 17,d1d7 a5e1 d7d1 e1e3 c1b1 e3b6,2235,76,97,64,"
    "advantage fork long,https://lichess.org/kiuvTFoE#33,Sicilian_Defense Sicilian_Defense_Dragon_Variation,",
    "00sO1,1k1r4/pp3pp1/2p1p3/4b3/P3n1P1/8/KPP2PN1/3rBR1R b - - 2 31,b8c7 e1a5 b7b6 f1d1,998,85,94,293,"
    "advantage discoveredAttack master middlegame short,https://lichess.org/vsfFkG0s/black#62,,",
]

START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

# Synthetic rows to exercise the filters (positions don't matter here).
SYNTHETIC_ROWS = [
    f"T1300,{START},e2e4 e7e5,1300,80,90,500,fork short,,,",
    f"T1450,{START},e2e4 e7e5,1450,80,90,500,fork skewer mate,,,",
    f"T1600,{START},e2e4 e7e5,1600,80,90,500,skewer,,,",
    f"T1601,{START},e2e4 e7e5,1601,80,90,500,fork,,,",
    f"T1299,{START},e2e4 e7e5,1299,80,90,500,fork,,,",
    # Theme that contains another theme's name as a substring.
    f"T1500,{START},e2e4 e7e5,1500,80,90,500,mateIn2 smotheredMate,,,",
    # Low quality, and empty optional numeric fields.
    f"TLOWQ,{START},e2e4 e7e5,1400,,10,5,fork,,,",
    f"TNULL,{START},e2e4 e7e5,1400,,,,,,,",
]


def write_csv(path: Path, rows: list[str], header: str = HEADER) -> Path:
    path.write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path
