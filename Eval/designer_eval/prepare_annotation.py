#!/usr/bin/env python3
"""Create blind annotation sheets and an adjudication sheet.

The proposed labels are stored in ``queries/designer_queries.csv`` for the
harness, but the two annotators must not see them.  This script writes two
independent sheets containing only the query text, plus an adjudication sheet
that contains the proposed label for the final discussion.
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path

from Eval.designer_eval.common import CATEGORIES, normalize_category, read_csv, write_csv


HERE = Path(__file__).resolve().parent
DEFAULT_QUERIES = HERE / "queries" / "designer_queries.csv"
DEFAULT_OUT = HERE / "annotations"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare Designer annotation sheets")
    parser.add_argument("--queries", default=str(DEFAULT_QUERIES))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT))
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--force", action="store_true",
                        help="Overwrite existing annotation files")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = read_csv(args.queries)

    counts = {category: 0 for category in CATEGORIES}
    for row in rows:
        category = normalize_category(row.get("gold_category"))
        if category in counts:
            counts[category] += 1
        row["gold_category"] = category
    if any(count != 15 for count in counts.values()):
        raise SystemExit(
            f"Expected 15/15/15 proposed queries, found {counts}. "
            "Fix the seed file before creating annotation sheets."
        )

    rng = random.Random(args.seed)
    rng.shuffle(rows)

    annotator_rows = [
        {
            "query_id": row["query_id"],
            "query": row["query"],
            "category": "",
            "confidence": "",
            "notes": "",
        }
        for row in rows
    ]
    adjudication_rows = [
        {
            "query_id": row["query_id"],
            "query": row["query"],
            "proposed_category": row["gold_category"],
            "subtype": row.get("subtype", ""),
            "expected_behavior": row.get("expected_behavior", ""),
            "adjudicated_category": "",
            "adjudicator_notes": "",
        }
        for row in rows
    ]

    targets = {
        out_dir / "annotator_A.csv": annotator_rows,
        out_dir / "annotator_B.csv": annotator_rows,
        out_dir / "adjudicated.csv": adjudication_rows,
    }
    for path, data in targets.items():
        if path.exists() and not args.force:
            raise SystemExit(f"{path} already exists; use --force to overwrite")
    write_csv(
        out_dir / "annotator_A.csv",
        annotator_rows,
        ["query_id", "query", "category", "confidence", "notes"],
    )
    write_csv(
        out_dir / "annotator_B.csv",
        annotator_rows,
        ["query_id", "query", "category", "confidence", "notes"],
    )
    write_csv(
        out_dir / "adjudicated.csv",
        adjudication_rows,
        [
            "query_id", "query", "proposed_category", "subtype",
            "expected_behavior", "adjudicated_category", "adjudicator_notes",
        ],
    )

    print(f"Created annotation sheets in {out_dir}")
    print("  annotator_A.csv   (blind, no proposed label)")
    print("  annotator_B.csv   (blind, no proposed label)")
    print("  adjudicated.csv   (proposed label visible; fill adjudicated_category)")
    print("\nAfter adjudication, copy the final labels into")
    print(f"  {args.queries}")
    print("and set label_status=adjudicated for all 45 rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
