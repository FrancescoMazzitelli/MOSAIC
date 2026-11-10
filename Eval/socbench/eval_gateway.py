"""Evaluate SOCBench-D through the running MOSAIC db-gateway.

This is the end-to-end variant of ``evaluate.py``: the catalog lives in
MongoDB/Qdrant and retrieval is performed by ``POST /index/search`` with
``return_endpoints=true`` (the Eq. 1 per-operation ranking added for this
benchmark). No LLM generation is involved.

Protocol is identical to Pesl et al. and to ``evaluate.py``:
  * in-domain condition: the Stage 1 filter is restricted to the five services
    of the query's domain;
  * per-query recall    = |retrieved_k INTERSECT gold| / |gold|;
  * per-query precision = |retrieved_k INTERSECT gold| / |retrieved_k|;
  * mean over all 550 queries;
  * duplicate ``METHOD /path`` ids are collapsed keeping the best score,
    matching Pesl's ``_get_endpoints_from``.

Usage::

    python eval_gateway.py                      # all 550 queries
    python eval_gateway.py --max-cases 1        # 10 queries (smoke test)
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import pandas as pd
import requests
from tqdm import tqdm

from gateway_common import GATEWAY_URL, cases_for, service_uid

PESL_BGE_REFERENCE = {
    "Endpoint split thin": {5: (49.8, 39.6), 10: (65.2, 27.2), 20: (83.7, 18.5)},
    "Endpoint split field": {5: (41.8, 33.1), 10: (52.7, 21.7), 20: (76.3, 16.6)},
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instances", nargs="*", default=None)
    parser.add_argument("--max-cases", type=int, default=None)
    parser.add_argument("--k", nargs="+", type=int, default=[5, 10, 20])
    parser.add_argument("--out-dir", type=Path, default=Path("results"))
    parser.add_argument("--tag", default="gateway")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="skip queries already present in <tag>_partial.jsonl",
    )
    parser.add_argument("--gateway-url", default=GATEWAY_URL)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=600.0)
    return parser.parse_args()


def search(query: str, service_ids: list[str], args: argparse.Namespace) -> list[dict]:
    payload = {
        "query": query,
        "service_ids": service_ids,
        "return_endpoints": True,
    }
    last_error: Exception | None = None
    for attempt in range(args.retries):
        try:
            response = requests.post(
                f"{args.gateway_url}/index/search",
                json=payload,
                timeout=args.timeout,
            )
            response.raise_for_status()
            return response.json().get("ranked_endpoints", [])
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"search failed after {args.retries} attempts: {last_error}")


def collapse(ranked_endpoints: list[dict], top_k: int) -> list[str]:
    """Dedup METHOD/path keeping the best RRF (tie: score, then id)."""
    best: dict[str, tuple[float, float]] = {}
    for row in ranked_endpoints:
        op_id = row["endpoint"]
        value = (float(row["rrf"]), float(row.get("score", 0.0)))
        previous = best.get(op_id)
        if previous is None or value > previous:
            best[op_id] = value
    ordered = sorted(best.items(), key=lambda item: (-item[1][0], -item[1][1], item[0]))
    return [op_id for op_id, _ in ordered[:top_k]]


def metrics(ranked: list[str], gold: tuple[str, ...], k: int) -> tuple[float, float, int]:
    top = ranked[:k]
    hits = len(set(top).intersection(gold))
    recall = hits / len(gold) if gold else 0.0
    precision = hits / len(top) if top else 0.0
    return recall, precision, len(top)


def main() -> None:
    args = parse_args()
    cases = cases_for(instances=args.instances, max_cases=args.max_cases)
    if not cases:
        raise SystemExit("no benchmark case found")
    print(
        f"cases={len(cases)} queries={sum(len(c.queries) for c in cases)} "
        f"k={args.k} gateway={args.gateway_url}"
    )

    max_k = max(args.k)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = args.out_dir / f"{args.tag}_partial.jsonl"
    if not args.resume and checkpoint.exists():
        checkpoint.unlink()

    rows: list[dict] = []
    done: set[tuple[str, str, int]] = set()
    if args.resume and checkpoint.exists():
        with checkpoint.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                query_rows = payload.get("rows", [])
                if not query_rows:
                    continue
                first = query_rows[0]
                done.add((first["instance"], first["domain"], first["query_index"]))
                rows.extend(query_rows)
        print(f"resumed {len(done)} queries from {checkpoint}")

    started = time.time()
    checkpoint_fh = checkpoint.open("a", encoding="utf-8")
    try:
        for case in tqdm(cases, desc="domains"):
            service_ids = [service_uid(case, service) for service in case.services]
            for query in case.queries:
                key = (case.instance, case.domain, query.index)
                if key in done:
                    continue
                ranked_endpoints = search(query.text, service_ids, args)
                ranked = collapse(ranked_endpoints, max_k)
                query_rows: list[dict] = []
                for k in args.k:
                    recall, precision, retrieved = metrics(ranked, query.gold, k)
                    row = {
                        "variant": "mosaic_gateway",
                        "instance": case.instance,
                        "domain": case.domain,
                        "query_index": query.index,
                        "k": k,
                        "recall": recall,
                        "precision": precision,
                        "n_gold": len(query.gold),
                        "n_retrieved": retrieved,
                        "n_candidates": len(ranked_endpoints),
                    }
                    query_rows.append(row)
                    rows.append(row)
                checkpoint_fh.write(json.dumps({"rows": query_rows}) + "\n")
                checkpoint_fh.flush()
    finally:
        checkpoint_fh.close()

    elapsed = time.time() - started
    frame = pd.DataFrame(rows)
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

    args.out_dir.mkdir(parents=True, exist_ok=True)
    frame.to_csv(args.out_dir / f"{args.tag}_per_query.csv", index=False)
    summary.to_csv(args.out_dir / f"{args.tag}_summary.csv", index=False)
    per_domain.to_csv(args.out_dir / f"{args.tag}_per_domain.csv", index=False)

    print(f"\nelapsed: {elapsed / 60:.1f} min")
    print("\n=== summary (mean over queries, %) ===")
    printable = summary.copy()
    printable["recall"] = (100 * printable["recall"]).round(1)
    printable["precision"] = (100 * printable["precision"]).round(1)
    print(printable.to_string(index=False))

    print("\n=== LaTeX row ===")
    subset = summary.set_index("k")
    cells: list[str] = []
    for k in args.k:
        cells.append(f"{100 * subset.loc[k, 'recall']:.1f}")
        cells.append(f"{100 * subset.loc[k, 'precision']:.1f}")
    print("MOSAIC (full stack, Qwen3-Embedding-0.6B + bge-reranker) & " + " & ".join(cells) + r" \\")

    print("\n=== reference: Pesl et al., BGE-small, as reported (%) ===")
    for name, values in PESL_BGE_REFERENCE.items():
        ref_cells = []
        for k in args.k:
            recall, precision = values[k]
            ref_cells.extend([f"{recall:.1f}", f"{precision:.1f}"])
        print(f"{name:22s} & " + " & ".join(ref_cells) + r" \\")

    print(f"\nfiles: {args.out_dir / (args.tag + '_per_query.csv')}")
    print(f"       {args.out_dir / (args.tag + '_summary.csv')}")
    print(f"       {args.out_dir / (args.tag + '_per_domain.csv')}")


if __name__ == "__main__":
    main()
