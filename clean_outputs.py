"""Delete old generated videos: keep the newest N job folders in outputs/.

Usage: python clean_outputs.py --keep 10 [--dry-run]
"""

import argparse
from pathlib import Path

from studio.storage import prune_outputs, storage_summary

OUTPUT_DIR = Path(__file__).resolve().parent / "outputs"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", type=int, default=10, help="number of newest jobs to keep (default 10)")
    parser.add_argument("--dry-run", action="store_true", help="show what would be deleted without deleting")
    args = parser.parse_args()

    before = storage_summary(OUTPUT_DIR)
    result = prune_outputs(OUTPUT_DIR, keep=args.keep, dry_run=args.dry_run)
    verb = "Would remove" if args.dry_run else "Removed"
    print(f"Before: {before['count']} jobs, {before['total_mb']} MB")
    print(f"{verb} {len(result['removed'])} jobs, freeing {result['freed_mb']} MB")
    for name in result["removed"]:
        print(f"  - {name}")


if __name__ == "__main__":
    main()
