# Printable Chess Puzzles

Pick chess puzzles from the [Lichess puzzle database](https://database.lichess.org/#puzzles)
(over 6 million puzzles) by rating and theme, and print them as a PDF: one puzzle per page,
newspaper style, with the answers at the end.

> **Printing:** the PDF is made for **A5 paper, printed double-sided**. See [Printing](#printing).

- Choose a rating range, themes to include (any or all), and themes to exclude.
- A built-in quality filter skips obscure or unpopular puzzles.
- Remembers which puzzles you already printed, so each run gives you new ones (unless you ask for repeats).
- Each PDF starts with a minimal cover: a large knight, the title, the number of puzzles, the rating range,
  the themes you chose (if any), the date and a set number. The set number is the run number in
  your history, so `python main.py history` tells you what was in "Set #12".
- Every page shows the board from the solver's side, whose move it is, the puzzle ID, rating,
  the opponent's last move, and lines to write your answer.
- The solutions are collected at the end of the PDF, 20 per page: the moves for each puzzle, with its themes.
  The themes appear only in the answer key, because they would give hints.

## Setup

You need **Python 3.10+** and some disk space:

| | Size |
|---|---|
| Download (`lichess_db_puzzle.csv.zst`) | about 300 MB (the extracted CSV would be about 1.1 GB, but you don't need to extract it) |
| Database (`data/chess_puzzles.db`) | about 2.2 GB |
| Free space needed during the first import | about **4 GB** (the database plus temporary sorting files) |
| Free space needed to re-import later | about **7 GB**, because the old database, its backup and the new one exist at the same time (use `--no-backup` to need less) |

After the import, the downloaded `.zst` file is no longer needed and can be deleted
(see [Freeing disk space](#freeing-disk-space)).

### 1. Clone and install

```bash
git clone https://github.com/daniel-winkler/printable-chess-puzzles.git
cd printable-chess-puzzles
python -m venv .venv
```

Activate the virtual environment:

| System | Command |
|---|---|
| Windows (PowerShell) | `.venv\Scripts\Activate.ps1` |
| Windows (cmd) | `.venv\Scripts\activate.bat` |
| macOS / Linux | `source .venv/bin/activate` |

Then install the dependencies:

```bash
pip install -r requirements.txt
```

### 2. Download the puzzles

Download `lichess_db_puzzle.csv.zst` from <https://database.lichess.org/#puzzles> and put it in the
`data/` folder. You don't need to decompress it, but an uncompressed `lichess_db_puzzle.csv` (or
`puzzles.csv`) works too.

### 3. Build the database (once)

```bash
python scripts/import_database.py
```

This reads the CSV into `data/chess_puzzles.db` (SQLite). It takes about **5 minutes** and runs in 3 steps:

```
  Step 1/3  Reading CSV:  42.0%  (2,600,000 puzzles, 1m 03s elapsed, ~1m 27s left)
  Step 2/3  Building the theme table (28,034,726 rows, no progress shown, usually 1-3 minutes)...
  Step 3/3  Creating the index and checking the result...
```

You can also pass a path: `python scripts/import_database.py path/to/lichess_db_puzzle.csv.zst`.

## Usage

### Generate a PDF

```bash
python main.py generate --count 100 --min-rating 1300 --max-rating 1600 --themes fork skewer
```

The PDF is written to `output/puzzles_<date>_<time>.pdf` (or use `-o my_puzzles.pdf`). A run of
100 puzzles takes a few seconds: selecting them takes under a second, and writing the PDF about 2 seconds.

| Option | Meaning |
|---|---|
| `-n`, `--count N` | Number of puzzles (default 100) |
| `--min-rating`, `--max-rating` | Rating range, inclusive (default: all) |
| `--themes T1 T2 ...` | Only puzzles with **any** of these themes |
| `--match-all-themes` | Require **all** of `--themes` instead |
| `--exclude-themes T1 T2 ...` | Skip puzzles with any of these themes |
| `--allow-repeats` | Also allow puzzles you printed before |
| `--min-popularity N` | Quality filter: minimum popularity from -100 to 100 (default 80) |
| `--min-plays N` | Quality filter: minimum times played on Lichess (default 100) |
| `--no-quality-filter` | Turn the quality filter off |
| `--title "..."` | Title on the cover (default "Chess Puzzles") |
| `--no-record` | Don't save this run to the history (the cover then has no set number) |
| `-o`, `--output FILE` | Where to save the PDF |
| `--history FILE` | Use a different history file, e.g. `data/anna.db` to keep one history per person |

`python main.py generate --help` lists all options.

Theme names are not case-sensitive. The puzzles are picked at random from all matching puzzles.
The default quality filter keeps about three quarters of all puzzles.
If fewer puzzles match than you asked for, the PDF contains the ones that do and a message says so.

Puzzle lengths are themes too: `oneMove`, `short`, `long`, `veryLong`, `mateIn1` ... `mateIn5`.
The full list of themes is explained at <https://lichess.org/training/themes>.

More examples:

```bash
# Mates in 2 between 1000 and 1400
python main.py generate --min-rating 1000 --max-rating 1400 --themes mateIn2

# Endgames, but no pawn endgames
python main.py generate --themes endgame --exclude-themes pawnEndgame

# Puzzles that are both a sacrifice AND lead to mate, with your own cover title
python main.py generate --themes sacrifice mate --match-all-themes --title "Sacrifices that mate"

# 20 puzzles even if you've seen them before
python main.py generate -n 20 --min-rating 1500 --max-rating 1800 --allow-repeats
```

### Other commands

```bash
python main.py themes          # list every theme and how many puzzles have it
python main.py history         # list previous runs, their criteria and PDF paths
python main.py reset-history   # forget which puzzles you've printed
python main.py cleanup         # show which large files can be deleted to free disk space
```

### Freeing disk space

```bash
python main.py cleanup --downloads   # delete the downloaded .csv.zst / .csv (about 300 MB - 1.1 GB)
python main.py cleanup --database    # delete the puzzle database and its backup (about 2.2 GB each)
python main.py cleanup --all         # both
```

It asks for confirmation first (add `--yes` to skip). **Your history is never deleted**, so after
you download the puzzles again and re-import them, puzzles you already printed are still skipped.
You can delete the download as soon as the import has finished. Delete the database if you don't
plan to print puzzles for a while; `python main.py history` keeps working without it.

## Printing

The PDF is designed for **A5 paper, printed double-sided** (flip on the long edge). Other paper
sizes are not supported: the layout is made for A5.

In your print dialog, choose:
- **Paper size:** A5
- **Scale:** 100% / "Actual size" (not "Fit to page")
- **Double-sided / duplex:** on, **flip on long edge**

The page order is planned for double-sided printing:

| Pages | Content |
|---|---|
| 1 | Cover |
| 2 | Blank (back of the cover) |
| 3 … | One puzzle per page |
| *(blank)* | Added only when the number of puzzles is odd |
| last | Solutions |

So the puzzles start on their own sheet, and the solutions always start on the front of a new sheet.
You can take the answer sheets out without losing a puzzle on their back.

If your printer can't print double-sided, choose "manual duplex" in the print dialog if it offers it,
or print the odd pages first, put the stack back in the tray, then print the even pages.

## How it works

| File | Purpose |
|---|---|
| `scripts/import_database.py` | Imports the CSV into SQLite |
| `puzzle_database.py` | Filters puzzles and stores the history of printed puzzles |
| `pdf_renderer.py` | Draws the boards and writes the PDF |
| `main.py` | Command-line interface |
| `scripts/benchmark.py` | Times typical queries on your database |
| `tests/` | Automated tests (`python -m unittest discover tests`) |

**Database.** SQLite needs no server and keeps everything in one file. Each puzzle's themes are
also stored one per row in a `puzzle_themes` table, so theme searches match whole theme names only
(`mate` never matches `mateIn2`). That table also holds each puzzle's rating, popularity and play count,
and is sorted by theme and then rating. A search like "fork puzzles rated 1300–1600" therefore reads
one contiguous slice of it. Searches without a theme use an index on `puzzles` that holds the same
columns. On the full database (6.2 million puzzles) every search takes under a second;
`python scripts/benchmark.py` measures this on your computer.

**Importing safely.** The import builds a new database next to the old one (`chess_puzzles_staging.db`)
and checks it: required fields, numeric fields, duplicate IDs, row counts, and that every theme row
belongs to a puzzle. Only then does it replace
`data/chess_puzzles.db`. If anything fails, your existing database stays as it was. The previous
database is kept as `chess_puzzles_backup.db` (use `--no-backup` to skip this and save disk space).
To update to a newer Lichess download, just run the import again.

**History.** The puzzles you've printed are recorded in a separate file, `data/history.db`, so
re-importing the puzzle database never erases your history.

**Random selection.** Puzzles are chosen with `ORDER BY RANDOM()` over all matching puzzle IDs,
which gives a truly uniform random pick. Then only the selected puzzles are read in full.

**Reading the puzzles.** In the Lichess data, the FEN is the position *before* the opponent's move,
and the first move in `Moves` is that opponent move. The program plays that move, shows the
resulting position, and prints the rest of `Moves` as the solution in normal chess notation
(e.g. `18. Be6+ Kd8 19. Qf8#`).

## Running the tests

```bash
python -m unittest discover tests
```

## Data license

The Lichess puzzle database is released under the
[Creative Commons CC0](https://creativecommons.org/publicdomain/zero/1.0/) license.
