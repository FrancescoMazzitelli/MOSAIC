"""Build the final LaTeX table: Pesl et al. rows + the measured MOSAIC rows.

The Pesl values are copied from the authors' artifact
(``socbench-d/code/src/analysis/cross-domain-average.ipynb``, cells 10-14)
and rounded to one decimal, exactly as they appear in the paper table.

MOSAIC rows are read from the summary CSVs produced by the two runners:

  * ``results/gateway_summary.csv`` -- full Docker stack (MongoDB + Qdrant +
    db-gateway, Qwen3-Embedding-0.6B + bge-reranker-base), variant
    ``mosaic_gateway``;
  * ``results/bge_summary.csv`` -- standalone harness with BGE-small-en-v1.5,
    variant ``mosaic`` (same embedder as the BGE block);
  * ``results/qwen_summary.csv`` -- optional standalone Qwen row.

Usage::

    python make_table.py
    python make_table.py --gateway-summary results/gateway_summary.csv
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"
KS = (5, 10, 20)

STRATEGIES = [
    "Endpoint split (0)",
    "Endpoint split (20)",
    "Endpoint split thin",
    "Endpoint split field",
    "Endpoint split JSON",
    "Query",
    "Summary",
    "CRAFT",
]

# As reported by Pesl et al. (percent). Source: cross-domain-average.ipynb.
PESL = {
    "BGE-small-en-v1.5 (384d)": {
        "Endpoint split (0)": {5: (50.1, 39.9), 10: (64.7, 27.0), 20: (83.5, 18.4)},
        "Endpoint split (20)": {5: (50.1, 39.9), 10: (64.7, 27.0), 20: (83.5, 18.4)},
        "Endpoint split thin": {5: (49.8, 39.6), 10: (65.2, 27.2), 20: (83.7, 18.5)},
        "Endpoint split field": {5: (41.8, 33.1), 10: (52.7, 21.7), 20: (76.3, 16.6)},
        "Endpoint split JSON": {5: (50.2, 39.8), 10: (65.6, 27.3), 20: (83.5, 18.4)},
        "Query": {5: (44.6, 35.0), 10: (60.7, 25.1), 20: (79.5, 17.5)},
        "Summary": {5: (45.2, 35.8), 10: (63.4, 26.4), 20: (80.9, 17.7)},
        "CRAFT": {5: (46.7, 36.7), 10: (64.4, 27.0), 20: (82.3, 18.2)},
    },
    "OpenAI text-embedding-3-large (3072d)": {
        "Endpoint split (0)": {5: (52.1, 41.7), 10: (66.5, 28.1), 20: (86.0, 19.1)},
        "Endpoint split (20)": {5: (52.0, 41.8), 10: (66.7, 28.2), 20: (85.9, 19.1)},
        "Endpoint split thin": {5: (51.8, 41.3), 10: (65.5, 27.7), 20: (85.9, 19.1)},
        "Endpoint split field": {5: (47.9, 38.0), 10: (59.8, 24.8), 20: (82.7, 18.2)},
        "Endpoint split JSON": {5: (52.1, 41.8), 10: (67.1, 28.5), 20: (86.2, 19.2)},
        "Query": {5: (47.7, 37.4), 10: (64.8, 27.1), 20: (82.8, 18.3)},
        "Summary": {5: (50.9, 40.6), 10: (66.4, 28.1), 20: (83.7, 18.6)},
        "CRAFT": {5: (47.9, 37.7), 10: (64.6, 27.3), 20: (84.4, 18.8)},
    },
    "NVIDIA NV-Embed-v2 (4096d)": {
        "Endpoint split (0)": {5: (53.1, 42.8), 10: (68.2, 28.9), 20: (88.8, 19.9)},
        "Endpoint split (20)": {5: (53.1, 42.8), 10: (68.2, 28.9), 20: (88.8, 19.9)},
        "Endpoint split thin": {5: (52.9, 42.6), 10: (67.9, 28.8), 20: (88.3, 19.8)},
        "Endpoint split field": {5: (51.9, 42.2), 10: (63.4, 26.8), 20: (86.4, 19.2)},
        "Endpoint split JSON": {5: (54.6, 44.0), 10: (69.9, 29.7), 20: (89.2, 20.0)},
        "Query": {5: (48.7, 38.7), 10: (67.6, 28.4), 20: (85.3, 18.9)},
        "Summary": {5: (55.3, 44.7), 10: (73.5, 31.5), 20: (89.9, 20.3)},
        "CRAFT": {5: (49.5, 39.5), 10: (69.4, 29.4), 20: (87.5, 19.5)},
    },
}


def _read_summary(path: Path, variant: str) -> dict[int, tuple[float, float]]:
    if not path.exists():
        raise SystemExit(f"missing {path}")
    values: dict[int, tuple[float, float]] = {}
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            if row["variant"] != variant:
                continue
            values[int(row["k"])] = (
                100 * float(row["recall"]),
                100 * float(row["precision"]),
            )
    if set(values) != set(KS):
        raise SystemExit(f"{path}: expected k={KS}, found {sorted(values)}")
    return values


def _row(name: str, values: dict[int, tuple[float, float]]) -> str:
    cells: list[str] = []
    for k in KS:
        recall, precision = values[k]
        cells.extend([f"{recall:.1f}", f"{precision:.1f}"])
    return f"{name} & " + " & ".join(cells) + r" \\"


def build_table(
    summary_path: Path | None = None,
    variant: str = "mosaic",
    gateway_path: Path | None = None,
) -> str:
    lines = [
        r"\begin{table*}[t]",
        r"\centering",
        r"\caption{Endpoint discovery on SOCBench-D: recall (R) and precision (P) in \%, "
        r"as reported by Pesl \emph{et al.}~\cite{pesl2026}. Values are averaged over the "
        r"five benchmark instances and the eleven GICS domains, following the authors' "
        r"protocol. Each block is a different embedding model; each row is a different "
        r"OpenAPI chunking strategy. The MOSAIC block is the proposed system, evaluated "
        r"under the same protocol and in-domain condition (five services per query); the "
        r"remaining differences with the Pesl rows are the retrieval architecture "
        r"(two-stage retrieval with a cross-encoder and RRF, Eq.~1) and the endpoint "
        r"representation.}",
        r"\label{tab:socbench-full}",
        r"\scriptsize",
        r"\setlength{\tabcolsep}{3.6pt}",
        r"\renewcommand{\arraystretch}{0.94}",
        r"\begin{tabular}{@{}lrrrrrr@{}}",
        r"\toprule",
        r" & \multicolumn{2}{c}{$k=5$} & \multicolumn{2}{c}{$k=10$} & \multicolumn{2}{c}{$k=20$}\\",
        r"\cmidrule(lr){2-3}\cmidrule(lr){4-5}\cmidrule(lr){6-7}",
        r"Chunking strategy & R & P & R & P & R & P\\",
        r"\midrule",
    ]

    for block, values in PESL.items():
        lines.append(rf"\multicolumn{{7}}{{@{{}}l}}{{\emph{{{block}}}}}\\")
        for strategy in STRATEGIES:
            lines.append(_row(strategy, values[strategy]))
        lines.append(r"\midrule")

    if summary_path is not None and summary_path.exists():
        bge = _read_summary(summary_path, variant)
        lines.append(
            r"\multicolumn{7}{@{}l}{\emph{MOSAIC (this work, BGE-small-en-v1.5)}}\\"
        )
        lines.append(_row(r"\quad two-stage retrieval + RRF (Eq.~1)", bge))

    qwen_path = RESULTS / "qwen_summary.csv"
    if qwen_path.exists():
        qwen = _read_summary(qwen_path, "mosaic")
        lines.append(
            r"\multicolumn{7}{@{}l}{\emph{MOSAIC (this work, Qwen3-Embedding-0.6B)}}\\"
        )
        lines.append(_row(r"\quad two-stage retrieval + RRF (Eq.~1)", qwen))

    if gateway_path is not None and gateway_path.exists():
        gateway = _read_summary(gateway_path, "mosaic_gateway")
        lines.append(
            r"\multicolumn{7}{@{}l}{\emph{MOSAIC full stack (this work, "
            r"Qwen3-Embedding-0.6B + bge-reranker-base)}}\\"
        )
        lines.append(
            _row(r"\quad MongoDB + Qdrant, two-stage retrieval + RRF (Eq.~1)", gateway)
        )

    lines.extend(
        [
            r"\bottomrule",
            r"\end{tabular}",
            r"",
            r"\vspace{2pt}",
            r"{\footnotesize Note: $|E|\approx5$, hence $P@20 \le |E|/20 \approx 0.25$; "
            r"the low $P@20$ is structural, not a retrieval failure.}",
            r"\end{table*}",
        ]
    )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, default=RESULTS / "bge_summary.csv")
    parser.add_argument("--variant", default="mosaic")
    parser.add_argument(
        "--gateway-summary", type=Path, default=RESULTS / "gateway_summary.csv"
    )
    parser.add_argument("--out", type=Path, default=RESULTS / "table_socbench.tex")
    args = parser.parse_args()

    summary = args.summary if args.summary.exists() else None
    if summary is None:
        print(f"note: {args.summary} not found, skipping the standalone BGE row")
    table = build_table(summary, args.variant, args.gateway_summary)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(table)
    print(table)
    print(f"written: {args.out}")
