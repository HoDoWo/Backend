"""Load the curated marketplace seed or import partner feeds into Firestore.

Run it from the repository root, with the Firebase variables set in .env:

    # curated seed (#33): partners, products, parts and compatibility
    python scripts/import_partner_feed.py --seed --dry-run
    python scripts/import_partner_feed.py --seed

    # one or more partner feeds (#24), CSV or JSON
    python scripts/import_partner_feed.py --feed voltacasa=feeds/voltacasa.csv --dry-run
    python scripts/import_partner_feed.py --feed voltacasa=feeds/voltacasa.csv --feed nordhome=feeds/nordhome.json

    # the feed is the partner's whole catalogue: missing products go out of stock
    python scripts/import_partner_feed.py --feed voltacasa=feeds/voltacasa.csv --full --dry-run

Running it twice never duplicates documents. With --dry-run nothing is
written and the report shows what would change. The exit code is 1 when a feed
fails or the seed is incomplete, 0 otherwise.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "api"))

import catalogue_import  # noqa: E402
import config  # noqa: E402,F401  (loads .env)

SEED_DIR = ROOT / "scripts" / "seed"


def _feed(value: str) -> tuple[str, pathlib.Path]:
    partner_id, sep, path = value.partition("=")
    if not sep or not partner_id or not path:
        raise argparse.ArgumentTypeError("use --feed PARTNER_ID=path/to/feed.csv")
    return partner_id, pathlib.Path(path)


def main(argv: list[str] | None = None, store=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seed", action="store_true", help=f"load the curated seed from {SEED_DIR.relative_to(ROOT)}")
    parser.add_argument("--seed-dir", type=pathlib.Path, default=SEED_DIR, help=argparse.SUPPRESS)
    parser.add_argument("--feed", type=_feed, action="append", default=[], metavar="PARTNER_ID=FILE",
                        help="import a partner feed (repeatable)")
    parser.add_argument("--full", action="store_true",
                        help="the feeds are the partners' whole catalogue: products missing from them go out of stock")
    parser.add_argument("--dry-run", action="store_true", help="show what would change without writing")
    parser.add_argument("--verbose", "-v", action="store_true", help="list every changed document")
    args = parser.parse_args(argv)
    if not args.seed and not args.feed:
        parser.error("choose --seed and/or at least one --feed")

    failed = False
    if args.seed:
        problems = catalogue_import.check_seed(args.seed_dir)
        for problem in problems:
            print(f"SEED INCOMPLETE: {problem}")
        failed |= bool(problems)

    store = store or catalogue_import.FirestoreCatalogue()
    reports = catalogue_import.import_seed(store, args.seed_dir, dry_run=args.dry_run) if args.seed else []
    partners = store.read("partners") if args.feed else None
    for partner_id, path in args.feed:
        reports.append(catalogue_import.import_partner_feed(
            store, partner_id, path, dry_run=args.dry_run, full=args.full, partners=partners))

    print("DRY RUN: nothing was written." if args.dry_run else "Import finished.")
    for report in reports:
        print(report.summary())
        # Rejected rows are always shown; the full diff with --verbose or --dry-run.
        details = report.details() if (args.verbose or args.dry_run) else [f"  ! {r}" for r in report.rejected]
        for line in details:
            print(line)
        failed |= report.failed
    if args.seed and any(report.rejected for report in reports):
        print("SEED INCOMPLETE: the seed files contain rejected rows")
        failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
