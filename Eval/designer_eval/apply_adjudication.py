#!/usr/bin/env python3
"""Apply adjudicated labels to the query CSV.

Typical flow:

    # after filling annotations/adjudicated.csv
    python3 apply_adjudication.py
    # -> queries/designer_queries_adjudicated.csv

Then run/analyse with ``--queries`` pointing to the adjudicated file, or use
``--in-place`` once the adjudication is final.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from Eval.designer_eval.common import CATEGORIES, normalize_category, read_csv, write_csv


HERE = Path(__file__).resolve().parent
DEFAULT_QUERIES = HERE / "queries" / "designer_queries.csv"
DEFAULT_ADJUDICATED = HERE / "annotations" / "adjudicated.csv"
DEFAULT_OUTPUT = HERE / "queries" / "designer_queries_adjudicated.csv"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply adjudicated Designer labels")
    parser.add_argument("--queries", default=str(DEFAULT_QUERIES))
    parser.add_argument("--adjudicated", default=str(DEFAULT_ADJUDICATED))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--in-place", action="store_true",
                        help="Overwrite --queries instead of writing --output")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    queries = read_csv(args.queries)
    adjudicated = {
        row["query_id"]: row for row in read_csv(args.adjudicated)
        if row.get("query_id")
    }
    if not adjudicated:
        raise SystemExit(f"No rows found in {args.adjudicated}")

    missing: list[str] = []
    invalid: list[str] = []
    updated: list[dict[str, str]] = []
    for row in queries:
        row = dict(row)
        label = adjudicated.get(row["query_id"])
        if not label:
            missing.append(row["query_id"])
            continue
        category = normalize_category(label.get("adjudicated_category"))
        if category not in CATEGORIES:
            invalid.append(f"{row['query_id']}={label.get('adjudicated_category')!r}")
            continue
        row["gold_category"] = category
        row["label_status"] = "adjudicated"
        note = (label.get("adjudicator_notes") or "").strip()
        if note:
            old_note = (row.get("notes") or "").strip()
            row["notes"] = f"{old_note} | adjudication: {note}" if old_note else note
        updated.append(row)

    if missing:
        raise SystemExit(
            "Missing adjudication for: " + ", ".join(sorted(missing))
        )
    if invalid:
        raise SystemExit(
            "Invalid adjudicated categories: " + "; ".join(invalid)
        )

    output = Path(args.queries) if args.in_place else Path(args.output)
    fields = [
        "query_id", "query", "gold_category", "subtype",
        "expected_behavior", "source", "contamination_flag",
        "label_status", "notes",
    ]
    write_csv(output, updated, fields)
    print(f"Wrote {len(updated)} adjudicated rows to {output}")
    print("All labels are now label_status=adjudicated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
