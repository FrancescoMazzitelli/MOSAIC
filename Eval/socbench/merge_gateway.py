"""Merge the five per-instance gateway runs into the final CSVs.

The full evaluation is run as five chunks (one per SOCBench-D instance) so
that an interruption only loses one chunk. This script concatenates the
per-query CSVs and recomputes the summary exactly as eval_gateway.py would
have done on the full set.

Usage::

    python merge_gateway.py
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

RESULTS = Path(__file__).resolve().parent / "results"
INSTANCES = (1, 2, 3, 4, 5)


def main() -> None:
    frames = []
    for index in INSTANCES:
        path = RESULTS / f"gateway_{index}_per_query.csv"
        if not path.exists():
            raise SystemExit(f"missing {path}; run the corresponding chunk first")
        frames.append(pd.read_csv(path))

    frame = pd.concat(frames, ignore_index=True)
    summary = (
        frame.groupby(["variant", "k"], as_index=False)
        .agg(
            recall=("recall", "mean"),
            precision=("precision", "mean"),
            n_queries=("recall", "size"),
        )
        .sort_values(["variant", "k"])
    )
    per_domain = (
        frame.groupby(["variant", "instance", "domain", "k"], as_index=False)
        .agg(
            recall=("recall", "mean"),
            precision=("precision", "mean"),
            n_queries=("recall", "size"),
        )
        .sort_values(["variant", "instance", "domain", "k"])
    )

    frame.to_csv(RESULTS / "gateway_per_query.csv", index=False)
    summary.to_csv(RESULTS / "gateway_summary.csv", index=False)
    per_domain.to_csv(RESULTS / "gateway_per_domain.csv", index=False)

    print(f"queries={frame['query_index'].nunique()} rows={len(frame)}")
    print("\n=== summary (mean over queries, %) ===")
    printable = summary.copy()
    printable["recall"] = (100 * printable["recall"]).round(1)
    printable["precision"] = (100 * printable["precision"]).round(1)
    print(printable.to_string(index=False))

    print("\n=== LaTeX row ===")
    subset = summary.set_index("k")
    cells: list[str] = []
    for k in (5, 10, 20):
        cells.append(f"{100 * subset.loc[k, 'recall']:.1f}")
        cells.append(f"{100 * subset.loc[k, 'precision']:.1f}")
    print(
        "MOSAIC full stack (Qwen3-Embedding-0.6B + bge-reranker-base) & "
        + " & ".join(cells)
        + r" \\"
    )
    print(f"\nwrote {RESULTS / 'gateway_summary.csv'}")


if __name__ == "__main__":
    main()
