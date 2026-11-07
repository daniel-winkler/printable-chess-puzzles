"""Turn puzzle rows into a printable PDF: one puzzle per page, answer key at the end.

Lichess puzzle format: FEN is the position *before* the opponent's last move.
The first move in `Moves` is that opponent move; the solver's line starts with
the second move. So the diagram shows the position after Moves[0], from the
solver's side of the board.
"""

from __future__ import annotations

import io
from functools import lru_cache
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import chess
import chess.svg
from reportlab.graphics import renderPDF
from reportlab.lib.pagesizes import A4, A5
from reportlab.lib.units import cm
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas
from svglib.svglib import svg2rlg

PAGE_SIZE = A5  # the only supported size: printed double-sided on A5 paper

# Printer-friendly greyscale board (newspaper style), as grey levels (0 = black, 1 = white).
LIGHT_SQUARE = 1.0
DARK_SQUARE = 0.72
COORD_ON_LIGHT = 0.5       # coordinate text on light squares
COORD_ON_DARK = 0.3        # coordinate text on dark squares (white would be too faint in print)
BORDER_WIDTH = 0.8         # pt, the frame around the board

FONT = "Helvetica"
FONT_BOLD = "Helvetica-Bold"


class PuzzleError(ValueError):
    """Raised when a puzzle's FEN or moves cannot be played out."""


@dataclass
class PreparedPuzzle:
    puzzle_id: str
    rating: int
    themes: list[str]
    board: chess.Board          # position the solver faces
    solver: chess.Color
    last_move_san: str          # opponent's move that led to the position, e.g. "17...Kd7"
    solution_san: str           # e.g. "18. Be6+ Kd8 19. Qf8#"

    @property
    def to_move(self) -> str:
        return "White to move" if self.solver == chess.WHITE else "Black to move"

    @property
    def lichess_url(self) -> str:
        return f"https://lichess.org/training/{self.puzzle_id}"


def prepare_puzzle(row: dict) -> PreparedPuzzle:
    """Play the setup move and convert the solution to readable notation."""
    puzzle_id = row["PuzzleId"]
    try:
        board = chess.Board(row["FEN"])
        moves = [chess.Move.from_uci(m) for m in row["Moves"].split()]
        if len(moves) < 2:
            raise ValueError("puzzle needs a setup move and at least one solution move")
        last_move_san = board.variation_san(moves[:1])  # raises if illegal
        board.push(moves[0])
        solution_san = board.variation_san(moves[1:])
    except ValueError as exc:
        raise PuzzleError(f"Puzzle {puzzle_id}: {exc}") from None
    return PreparedPuzzle(
        puzzle_id=puzzle_id,
        rating=row["Rating"],
        themes=(row.get("Themes") or "").split(),
        board=board,
        solver=board.turn,
        last_move_san=last_move_san,
        solution_san=solution_san,
    )


@lru_cache(maxsize=None)
def _piece_drawing(symbol: str):
    """The python-chess piece image for e.g. 'N' or 'p', converted once and reused."""
    svg = chess.svg.piece(chess.Piece.from_symbol(symbol))
    return svg2rlg(io.BytesIO(svg.encode("utf-8")))


def draw_board(c: canvas.Canvas, board: chess.Board, orientation: chess.Color,
               x: float, y: float, size: float) -> None:
    """Draw the board with its bottom-left corner at (x, y), `size` points wide.

    Coordinates sit inside the board on two sides: rank numbers in the top-left
    corner of the left column, file letters in the bottom-right corner of the
    bottom row. A thin frame separates the white squares from the white page.
    """
    sq = size / 8
    coord_size = sq * 0.2
    pad = sq * 0.06
    for file in range(8):
        for rank in range(8):
            col = file if orientation == chess.WHITE else 7 - file
            row = rank if orientation == chess.WHITE else 7 - rank
            sx, sy = x + col * sq, y + row * sq
            light = (file + rank) % 2 == 1
            c.setFillGray(LIGHT_SQUARE if light else DARK_SQUARE)
            c.rect(sx, sy, sq, sq, stroke=0, fill=1)

            piece = board.piece_at(chess.square(file, rank))
            if piece:
                drawing = _piece_drawing(piece.symbol())
                c.saveState()
                c.translate(sx, sy)
                c.scale(sq / drawing.width, sq / drawing.height)
                renderPDF.draw(drawing, c, 0, 0)
                c.restoreState()

            c.setFillGray(COORD_ON_LIGHT if light else COORD_ON_DARK)
            c.setFont(FONT_BOLD, coord_size)
            if col == 0:
                c.drawString(sx + pad, sy + sq - pad - coord_size * 0.75, chess.RANK_NAMES[rank])
            if row == 0:
                c.drawRightString(sx + sq - pad, sy + pad, chess.FILE_NAMES[file])

    c.setFillGray(0)
    c.setStrokeGray(0)
    c.setLineWidth(BORDER_WIDTH)
    c.rect(x, y, size, size, stroke=1, fill=0)


def _draw_wrapped(c: canvas.Canvas, text: str, x: float, y: float, max_width: float,
                  font: str, size: float, leading: float, centered: bool = False) -> float:
    """Draw word-wrapped text starting at baseline y; return the next free baseline.

    With centered=True, each line is centred within [x, x + max_width].
    """
    c.setFont(font, size)
    for line in simpleSplit(text, font, size, max_width):
        if centered:
            c.drawCentredString(x + max_width / 2, y, line)
        else:
            c.drawString(x, y, line)
        y -= leading
    return y


@dataclass
class Layout:
    """Page geometry and font sizes for the A5 page.

    The base sizes are A4 measurements scaled by A5/A4 (0.71x), with minimum
    font sizes so text stays readable in print.
    """
    page_w: float = PAGE_SIZE[0]
    page_h: float = PAGE_SIZE[1]

    def __post_init__(self):
        s = min(self.page_w / A4[0], 1.0)
        self.s = s
        self.margin = 1.8 * cm * s
        self.content_w = self.page_w - 2 * self.margin
        self.title = 20 * s
        self.to_move = max(16 * s, 12)
        self.body = max(11 * s, 9)
        self.leading = self.body * 1.35
        self.footer = max(8 * s, 6.5)
        self.board_w = min(self.content_w, 15.5 * cm * s)
        self.label_w = max(2.6 * cm * s, self.body * 5.6)
        self.writing_line_gap = max(26 * s, 20)

    def gap(self, points: float) -> float:
        return points * self.s


@dataclass
class CoverInfo:
    """What the cover page shows. set_number is the history run number (None if not recorded)."""
    puzzle_count: int
    min_rating: int = 0
    max_rating: int = 4000
    themes: list[str] = field(default_factory=list)
    match_all_themes: bool = False
    set_number: int | None = None
    title: str = "Chess Puzzles"
    date: datetime = field(default_factory=datetime.now)


_THEME_NAMES = {"xRayAttack": "X-ray attack", "superGM": "Super GM", "masterVsMaster": "Master vs master"}


def humanize_theme(theme: str) -> str:
    """'discoveredAttack' -> 'Discovered attack', 'mateIn2' -> 'Mate in 2'."""
    if theme in _THEME_NAMES:
        return _THEME_NAMES[theme]
    return re.sub(r"(?<=[a-z])(?=[A-Z0-9])", " ", theme).lower().capitalize()


def _rating_label(lo: int, hi: int) -> str:
    if lo <= 0 and hi >= 4000:
        return "All ratings"
    if hi >= 4000:
        return f"Rating {lo}+"
    if lo <= 0:
        return f"Rating up to {hi}"
    return f"Rating {lo}–{hi}"


def _wrap_spaced(text: str, font: str, size: float, char_space: float, max_w: float) -> list[str]:
    """Greedy word wrap that accounts for extra letter spacing."""
    def width(s: str) -> float:
        return stringWidth(s, font, size) + char_space * max(len(s) - 1, 0)

    lines, current = [], ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and width(candidate) > max_w:
            lines.append(current)
            current = word
        else:
            current = candidate
    return lines + ([current] if current else [])


def _draw_cover(c: canvas.Canvas, lay: Layout, info: CoverInfo) -> None:
    s = lay.s
    left, right = lay.margin, lay.page_w - lay.margin

    # One large knight, drawn with the same piece set as the boards.
    knight_w = 8.5 * cm * s
    svg = chess.svg.piece(chess.Piece(chess.KNIGHT, chess.BLACK), size=200)
    knight = svg2rlg(io.BytesIO(svg.encode("utf-8")))
    scale = knight_w / knight.width
    knight.scale(scale, scale)
    knight_top = lay.page_h - lay.margin - 3 * cm * s
    renderPDF.draw(knight, c, (lay.page_w - knight_w) / 2, knight_top - knight_w)

    # Title block between two rules, everything centred.
    y = knight_top - knight_w - 1.8 * cm * s
    c.setLineWidth(0.8)
    c.line(left, y, right, y)

    title_size = 30 * s
    char_space = title_size * 0.12
    y -= title_size * 1.5
    for line in _wrap_spaced(info.title.upper(), FONT_BOLD, title_size, char_space, right - left):
        line_w = stringWidth(line, FONT_BOLD, title_size) + char_space * (len(line) - 1)
        text = c.beginText((lay.page_w - line_w) / 2, y)
        text.setFont(FONT_BOLD, title_size)
        text.setCharSpace(char_space)
        text.textLine(line)
        text.setCharSpace(0)  # letter spacing persists in the PDF text state; reset it
        c.drawText(text)
        y -= title_size * 1.2

    sub_size = max(13 * s, 10)
    y -= sub_size * 0.6
    puzzles = f"{info.puzzle_count} puzzle" + ("" if info.puzzle_count == 1 else "s")
    y = _draw_wrapped(c, f"{puzzles}  ·  {_rating_label(info.min_rating, info.max_rating)}",
                      left, y, right - left, FONT, sub_size, sub_size * 1.4, centered=True)
    if info.themes:
        joiner = "  +  " if info.match_all_themes else "  ·  "
        y = _draw_wrapped(c, joiner.join(humanize_theme(t) for t in info.themes),
                          left, y, right - left, FONT, sub_size, sub_size * 1.4, centered=True)
    y -= sub_size * 0.4
    c.line(left, y, right, y)

    # Set number and date at the bottom, centred.
    small = max(11 * s, 9)
    bottom = lay.margin + small * 0.5
    c.setFont(FONT, small)
    c.drawCentredString(lay.page_w / 2, bottom, f"{info.date.day} {info.date:%B %Y}")
    if info.set_number is not None:
        c.setFont(FONT_BOLD, small)
        c.drawCentredString(lay.page_w / 2, bottom + small * 1.5, f"Set #{info.set_number}")


def _draw_footer(c: canvas.Canvas, lay: Layout, text: str) -> None:
    c.setFont(FONT, lay.footer)
    c.setFillGray(0.4)
    c.drawCentredString(lay.page_w / 2, lay.margin / 2, text)
    c.setFillGray(0)


def _draw_header(c: canvas.Canvas, lay: Layout, left: str, right: str = "") -> float:
    """Big title on the left, small text on the right, rule below; returns next y."""
    y = lay.page_h - lay.margin
    c.setFont(FONT_BOLD, lay.title)
    c.drawString(lay.margin, y - lay.title * 0.9, left)
    if right:
        c.setFont(FONT, lay.body)
        c.drawRightString(lay.page_w - lay.margin, y - lay.title * 0.8, right)
    y -= lay.title * 1.5
    c.setLineWidth(0.5)
    c.line(lay.margin, y, lay.page_w - lay.margin, y)
    return y


def _draw_puzzle_page(c: canvas.Canvas, lay: Layout, puzzle: PreparedPuzzle, number: int, total: int) -> None:
    y = _draw_header(c, lay, f"Puzzle {number}", f"ID: {puzzle.puzzle_id}")

    # Side to move, then the board.
    y -= lay.to_move * 1.6
    c.setFont(FONT_BOLD, lay.to_move)
    c.drawCentredString(lay.page_w / 2, y, puzzle.to_move)
    y -= lay.gap(20)
    y -= lay.board_w
    draw_board(c, puzzle.board, puzzle.solver, (lay.page_w - lay.board_w) / 2, y, lay.board_w)
    y -= lay.body * 2.2

    # Details. Themes are deliberately left out (they hint at the solution);
    # they are printed in the answer key instead.
    details = [
        ("Rating", str(puzzle.rating)),
        ("Last move", puzzle.last_move_san),
    ]
    for label, value in details:
        c.setFont(FONT_BOLD, lay.body)
        c.drawString(lay.margin, y, f"{label}:")
        y = _draw_wrapped(c, value, lay.margin + lay.label_w, y, lay.content_w - lay.label_w,
                          FONT, lay.body, lay.leading) - lay.gap(3)

    # Space to write the answer by hand.
    y -= lay.body
    c.setFont(FONT_BOLD, lay.body)
    c.drawString(lay.margin, y, "Your solution:")
    c.setLineWidth(0.4)
    c.setStrokeGray(0.6)
    line_y = y - lay.writing_line_gap
    while line_y > lay.margin + lay.gap(10):
        c.line(lay.margin, line_y, lay.page_w - lay.margin, line_y)
        line_y -= lay.writing_line_gap
    c.setStrokeGray(0)

    _draw_footer(c, lay, f"Puzzle {number} of {total}  -  {puzzle.lichess_url}")


SOLUTIONS_PER_PAGE = 20
SOLUTION_SIZE = 8         # pt, the moves
SOLUTION_MIN_SIZE = 6.5   # pt, long solutions shrink down to this before wrapping
THEME_SIZE = 7            # pt, the themes line
THEME_MIN_SIZE = 5.5


def _fit_size(text: str, font: str, size: float, min_size: float, width: float) -> float:
    """Largest font size <= size (but >= min_size) at which text fits on one line."""
    natural = stringWidth(text, font, size)
    return size if natural <= width else max(min_size, size * width / natural)


def _draw_answer_key(c: canvas.Canvas, lay: Layout, puzzles: list[PreparedPuzzle]) -> None:
    """Exactly SOLUTIONS_PER_PAGE entries per page, each in a fixed-height slot:
    puzzle number, the solution moves, and the themes underneath in grey."""
    indent = SOLUTION_SIZE * 3
    text_x = lay.margin + indent
    text_w = lay.content_w - indent

    for start in range(0, len(puzzles), SOLUTIONS_PER_PAGE):
        title = "Solutions" if start == 0 else "Solutions (continued)"
        top = _draw_header(c, lay, title) - lay.body * 0.8
        slot_h = (top - lay.margin) / SOLUTIONS_PER_PAGE

        for i, p in enumerate(puzzles[start:start + SOLUTIONS_PER_PAGE]):
            slot_top = top - i * slot_h

            # Solution: one line, shrunk to fit if long; two lines only if still too long.
            sol_size = _fit_size(p.solution_san, FONT, SOLUTION_SIZE, SOLUTION_MIN_SIZE, text_w)
            sol_lines = simpleSplit(p.solution_san, FONT, sol_size, text_w)
            if len(sol_lines) > 2:  # far longer than any real puzzle; never overflow the slot
                sol_lines = [sol_lines[0], sol_lines[1] + " …"]
            wrapped = len(sol_lines) > 1
            # A wrapped entry starts higher and uses tighter spacing to stay inside its slot.
            y = slot_top - (sol_size + 0.5 if wrapped else SOLUTION_SIZE + 2)
            c.setFont(FONT_BOLD, SOLUTION_SIZE)
            c.drawString(lay.margin, y, f"{start + i + 1}.")
            c.setFont(FONT, sol_size)
            for line in sol_lines:
                c.drawString(text_x, y, line)
                y -= sol_size * (1.1 if wrapped else 1.15)

            # Themes: one line, grey, shrunk to fit.
            themes = "Themes: " + (", ".join(p.themes) or "-")
            theme_size = _fit_size(themes, FONT, THEME_SIZE, THEME_MIN_SIZE, text_w)
            y -= 0 if wrapped else theme_size * 0.15
            c.setFillGray(0.4)
            c.setFont(FONT, theme_size)
            c.drawString(text_x, y, simpleSplit(themes, FONT, theme_size, text_w)[0])
            c.setFillGray(0)

        _draw_footer(c, lay, "Solutions")
        c.showPage()


def render_pdf(puzzles: list[PreparedPuzzle], output_path: str | Path,
               cover: CoverInfo | None = None, on_page=None) -> Path:
    """Write the A5 PDF, laid out for double-sided printing:

        cover, blank (back of the cover), one page per puzzle,
        blank if the number of puzzles is odd, answer key.

    So the puzzles start on a fresh sheet and the answer key starts on the
    front of a new sheet (it never shares a sheet with a puzzle).

    `cover` defaults to a plain cover for len(puzzles) puzzles.
    `on_page(number, total)` is called after each puzzle page, for progress display.
    """
    if not puzzles:
        raise ValueError("No puzzles to render")
    lay = Layout()
    cover = cover or CoverInfo(puzzle_count=len(puzzles))
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    c = canvas.Canvas(str(output_path), pagesize=(lay.page_w, lay.page_h))
    c.setTitle(cover.title + (f" - Set #{cover.set_number}" if cover.set_number is not None else ""))
    c.setAuthor("printable-chess-puzzles")
    _draw_cover(c, lay, cover)
    c.showPage()
    c.showPage()  # blank back of the cover
    for number, puzzle in enumerate(puzzles, start=1):
        _draw_puzzle_page(c, lay, puzzle, number, len(puzzles))
        c.showPage()
        if on_page:
            on_page(number, len(puzzles))
    if len(puzzles) % 2:
        c.showPage()  # blank, so the answer key starts on a new sheet
    _draw_answer_key(c, lay, puzzles)
    c.save()
    return output_path
