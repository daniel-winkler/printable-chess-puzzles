"""Select chess puzzles from the local database and print them to a PDF.

Examples:
    python main.py generate --count 100 --min-rating 1300 --max-rating 1600 --themes fork skewer
    python main.py generate --themes mate --exclude-themes mateIn1 --allow-repeats
    python main.py themes
    python main.py history
    python main.py reset-history
    python main.py cleanup --downloads

Run 'python main.py generate --help' for every option.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from pdf_renderer import CoverInfo, PuzzleError, prepare_puzzle, render_pdf
from puzzle_database import DEFAULT_DB_PATH, DEFAULT_HISTORY_PATH, PROJECT_ROOT, PuzzleDatabase
from scripts.import_database import DOWNLOAD_NAMES, backup_path_for, staging_path_for

# Quality filter defaults. Lichess popularity runs from -100 to 100; NbPlays is
# how many times the puzzle was attempted. Puzzles below these are often flawed
# or ambiguous.
DEFAULT_MIN_POPULARITY = 80
DEFAULT_MIN_PLAYS = 100


def resolve_themes(requested: list[str] | None, known: dict[str, int], option: str) -> list[str]:
    """Map user input to exact theme names (case-insensitive); fail on unknown names."""
    if not requested:
        return []
    by_lower = {name.lower(): name for name in known}
    resolved, unknown = [], []
    for theme in requested:
        match = by_lower.get(theme.strip().lower())
        (resolved if match else unknown).append(match or theme)
    if unknown:
        raise SystemExit(
            f"Unknown theme(s) for {option}: {', '.join(unknown)}\n"
            "Run 'python main.py themes' to see all available themes."
        )
    return resolved


def show_render_progress(number: int, total: int) -> None:
    """One updating line while drawing pages (only in an interactive terminal)."""
    if sys.stdout.isatty():
        print(f"\rRendering puzzle {number}/{total}...", end="\n" if number == total else "", flush=True)


def cmd_generate(args: argparse.Namespace) -> int:
    if args.count <= 0:
        raise SystemExit("--count must be a positive number")
    if args.no_quality_filter:
        min_popularity = min_plays = None
    else:
        min_popularity, min_plays = args.min_popularity, args.min_plays

    with PuzzleDatabase(args.db, args.history) as db:
        known = db.theme_counts()
        themes = resolve_themes(args.themes, known, "--themes")
        exclude = resolve_themes(args.exclude_themes, known, "--exclude-themes")

        criteria = {
            "min_rating": args.min_rating,
            "max_rating": args.max_rating,
            "themes": themes,
            "match_all_themes": args.match_all_themes,
            "exclude_themes": exclude,
            "min_popularity": min_popularity,
            "min_plays": min_plays,
            "exclude_seen": not args.allow_repeats,
        }
        print("Selecting puzzles...", flush=True)
        try:
            rows = db.get_puzzles(**criteria, limit=args.count, randomize=True)
        except ValueError as exc:
            raise SystemExit(f"Error: {exc}")

        if not rows:
            print("No puzzles match these criteria"
                  + ("" if args.allow_repeats else " that you have not already printed")
                  + ". Try widening the rating range, changing themes, using --allow-repeats "
                    "or --no-quality-filter.", file=sys.stderr)
            return 1
        if len(rows) < args.count:
            print(f"Only {len(rows)} puzzle(s) match"
                  + ("" if args.allow_repeats else " that you have not already printed")
                  + f" (asked for {args.count}). Stopping early with {len(rows)}.")

        prepared = []
        for row in rows:
            try:
                prepared.append(prepare_puzzle(row))
            except PuzzleError as exc:
                print(f"Skipping invalid puzzle: {exc}", file=sys.stderr)
        if not prepared:
            print("None of the selected puzzles could be rendered.", file=sys.stderr)
            return 1

        output = Path(args.output or PROJECT_ROOT / "output" / f"puzzles_{datetime.now():%Y%m%d_%H%M%S}.pdf")

        # Record first so the run number can be printed on the cover as the
        # set number; if writing the PDF fails, the run is removed again.
        run_id = None
        if not args.no_record:
            run_id = db.record_run([p.puzzle_id for p in prepared], criteria, str(output))
        cover = CoverInfo(
            puzzle_count=len(prepared),
            min_rating=args.min_rating,
            max_rating=args.max_rating,
            themes=themes,
            match_all_themes=args.match_all_themes,
            set_number=run_id,
            title=args.title,
        )
        try:
            path = render_pdf(prepared, output, cover=cover, on_page=show_render_progress)
        except BaseException:
            if run_id is not None:
                db.delete_run(run_id)
            raise

        if run_id is None:
            print("Not recorded in history (--no-record).")
        else:
            print(f"Recorded as set #{run_id}; {db.seen_count():,} puzzles printed so far.")

    print(f"Wrote {len(prepared)} puzzles + answer key to {path}")
    return 0


def cmd_themes(args: argparse.Namespace) -> int:
    with PuzzleDatabase(args.db, history_path=None) as db:
        counts = db.theme_counts()
    width = max(map(len, counts), default=0)
    for theme, n in counts.items():
        print(f"{theme:<{width}}  {n:>10,}")
    print(f"\n{len(counts)} themes")
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    with PuzzleDatabase(_db_if_present(args.db), args.history) as db:
        runs = db.list_runs()
        seen = db.seen_count()
    if not runs:
        print("No runs recorded yet.")
        return 0
    for run in runs:
        c = run["Criteria"]
        parts = [f"rating {c['min_rating']}-{c['max_rating']}"]
        if c.get("themes"):
            joiner = " AND " if c.get("match_all_themes") else " OR "
            parts.append("themes " + joiner.join(c["themes"]))
        if c.get("exclude_themes"):
            parts.append("excluding " + ", ".join(c["exclude_themes"]))
        if not c.get("exclude_seen", True):
            parts.append("repeats allowed")
        print(f"#{run['RunId']:<4} {run['CreatedAt']}  {run['PuzzleCount']:>4} puzzles  "
              f"{'; '.join(parts)}\n      -> {run['OutputPath']}")
    print(f"\n{seen:,} distinct puzzles printed in {len(runs)} run(s).")
    return 0


def cmd_reset_history(args: argparse.Namespace) -> int:
    if not args.yes:
        answer = input("Forget every previously printed puzzle? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Cancelled.")
            return 0
    with PuzzleDatabase(_db_if_present(args.db), args.history) as db:
        db.reset_history()
    print("History cleared.")
    return 0


def _db_if_present(db_path: Path) -> Path | None:
    """History commands work even after 'cleanup --database' removed the puzzle database."""
    return db_path if db_path.is_file() else None


def _cleanup_targets(db_path: Path) -> dict[str, list[Path]]:
    """Files that 'cleanup' may delete, grouped by option. Never includes the history."""
    data_dir = db_path.parent
    return {
        "downloads": [data_dir / name for name in DOWNLOAD_NAMES],
        "database": [db_path, backup_path_for(db_path), staging_path_for(db_path)],
    }


def _size(paths: list[Path]) -> int:
    return sum(p.stat().st_size for p in paths if p.is_file())


def _fmt_size(n: int) -> str:
    return f"{n / 2**30:.1f} GB" if n >= 2**30 else f"{n / 2**20:.0f} MB"


def cmd_cleanup(args: argparse.Namespace) -> int:
    targets = _cleanup_targets(args.db)
    labels = {
        "downloads": "Downloaded puzzle file (.csv / .csv.zst)",
        "database": "Puzzle database (and its backup)",
    }
    chosen = [k for k in targets if args.all or getattr(args, k)]

    if not chosen:
        print("Files that can be removed to free disk space (your history is always kept):\n")
        for key, paths in targets.items():
            present = [p for p in paths if p.is_file()]
            status = _fmt_size(_size(present)) if present else "not present"
            print(f"  --{key:<10} {labels[key]:<42} {status}")
        print("\nExample: python main.py cleanup --downloads")
        print("Both can be recreated: download the file again from https://database.lichess.org/#puzzles"
              " and run 'python scripts/import_database.py'.")
        return 0

    to_delete = [p for key in chosen for p in targets[key] if p.is_file()]
    if not to_delete:
        print("Nothing to remove.")
        return 0
    print("This will delete:")
    for p in to_delete:
        print(f"  {p}  ({_fmt_size(p.stat().st_size)})")
    if "database" in chosen:
        print("You will need to download the puzzles and run the import again before generating PDFs.")
    if not args.yes:
        answer = input("Continue? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Cancelled.")
            return 0
    freed = _size(to_delete)
    for p in to_delete:
        try:
            p.unlink()
        except PermissionError:
            print(f"Could not delete {p}: it is open in another program. Close it and try again.",
                  file=sys.stderr)
            return 1
    print(f"Freed {_fmt_size(freed)}. Your history ({args.history.name}) was kept.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Printable chess puzzles from the Lichess puzzle database.")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="Puzzle database file")
    common.add_argument("--history", type=Path, default=DEFAULT_HISTORY_PATH, help="History database file")
    sub = parser.add_subparsers(dest="command", required=True)

    g = sub.add_parser("generate", parents=[common], help="Select puzzles and write a PDF")
    g.add_argument("-n", "--count", type=int, default=100, help="Number of puzzles (default 100)")
    g.add_argument("--min-rating", type=int, default=0, help="Lowest rating, inclusive")
    g.add_argument("--max-rating", type=int, default=4000, help="Highest rating, inclusive")
    g.add_argument("--themes", nargs="+", metavar="THEME",
                   help="Only puzzles with ANY of these themes (see 'main.py themes')")
    g.add_argument("--match-all-themes", action="store_true",
                   help="Require ALL of --themes instead of any")
    g.add_argument("--exclude-themes", nargs="+", metavar="THEME",
                   help="Skip puzzles having any of these themes")
    g.add_argument("--allow-repeats", action="store_true",
                   help="Also pick puzzles printed in earlier runs")
    g.add_argument("--min-popularity", type=int, default=DEFAULT_MIN_POPULARITY,
                   help=f"Quality filter: minimum popularity, -100..100 (default {DEFAULT_MIN_POPULARITY})")
    g.add_argument("--min-plays", type=int, default=DEFAULT_MIN_PLAYS,
                   help=f"Quality filter: minimum number of plays (default {DEFAULT_MIN_PLAYS})")
    g.add_argument("--no-quality-filter", action="store_true",
                   help="Ignore popularity and play count")
    g.add_argument("-o", "--output", type=Path, help="PDF path (default output/puzzles_<timestamp>.pdf)")
    g.add_argument("--title", default="Chess Puzzles",
                   help='Title on the cover page (default "Chess Puzzles")')
    g.add_argument("--no-record", action="store_true",
                   help="Do not save this run to the history (puzzles may come up again)")
    g.set_defaults(func=cmd_generate)

    t = sub.add_parser("themes", parents=[common], help="List all themes and puzzle counts")
    t.set_defaults(func=cmd_themes)

    h = sub.add_parser("history", parents=[common], help="Show previous runs")
    h.set_defaults(func=cmd_history)

    r = sub.add_parser("reset-history", parents=[common], help="Forget which puzzles were printed")
    r.add_argument("-y", "--yes", action="store_true", help="Do not ask for confirmation")
    r.set_defaults(func=cmd_reset_history)

    cl = sub.add_parser("cleanup", parents=[common],
                        help="Delete the downloaded file and/or the puzzle database to free disk space")
    cl.add_argument("--downloads", action="store_true", help="Delete the downloaded .csv / .csv.zst file")
    cl.add_argument("--database", action="store_true",
                    help="Delete the puzzle database and its backup (re-import needed afterwards)")
    cl.add_argument("--all", action="store_true", help="Both of the above")
    cl.add_argument("-y", "--yes", action="store_true", help="Do not ask for confirmation")
    cl.set_defaults(func=cmd_cleanup)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
