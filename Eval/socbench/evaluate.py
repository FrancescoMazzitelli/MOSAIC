"""Evaluate MOSAIC's retrieval stage on SOCBench-D.

Protocol (identical to Pesl et al.):

  * per-query recall    = |retrieved_k INTERSECT gold| / |gold|
  * per-query precision = |retrieved_k INTERSECT gold| / |retrieved_k|
  * the reported values are the mean over all 550 queries
    (5 instances x 11 domains x 10 queries)

``retrieved_k`` is the list of *unique* operation ids in the top-k, so the
precision denominator matches Pesl's ``len(endpoints)`` after de-duplication.

Only the in-domain condition (5 services / 50 endpoints per query) is
comparable to the published table. A pooled condition would be a different
experiment and is intentionally not implemented here.
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd
from tqdm import tqdm

from data import load_cases
from models import Embedder, Reranker
from retrievers import flat_rank, mosaic_rank, reranker_rank

PESL_BGE_REFERENCE = {
    "Endpoint split thin": {5: (49.8, 39.6), 10: (65.2, 27.2), 20: (83.7, 18.5)},
    "Endpoint split field": {5: (41.8, 33.1), 10: (52.7, 21.7), 20: (76.3, 16.6)},
}

LABELS = {
    "mosaic": "MOSAIC two-stage + RRF",
    "reranker": "MOSAIC reranker only (ablation)",
    "flat": "Flat bi-encoder, same text (ablation)",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--embedder", choices=["bge", "qwen"], default="bge")
    parser.add_argument(
        "--variants",
        default="mosaic,reranker,flat",
        help="comma-separated subset of {mosaic,reranker,flat}",
    )
    parser.add_argument("--k", nargs="+", type=int, default=[5, 10, 20])
    parser.add_argument("--k1", type=int, default=7, help="stage-1 services")
    parser.add_argument("--k-rrf", type=int, default=60, help="RRF constant")
    parser.add_argument("--instances", nargs="*", default=None)
    parser.add_argument(
        "--max-cases",
        type=int,
        default=None,
        help="only the first N domain cases (smoke test)",
    )
    parser.add_argument("--out-dir", type=Path, default=Path("results"))
    parser.add_argument("--tag", default=None, help="output file prefix")
    return parser.parse_args()


def _metrics(ranked: list[str], gold: tuple[str, ...], k: int) -> tuple[float, float, int]:
    top = ranked[:k]
    hits = len(set(top).intersection(gold))
    recall = hits / len(gold) if gold else 0.0
    precision = hits / len(top) if top else 0.0
    return recall, precision, len(top)


def main() -> None:
    args = parse_args()
    variants = [item.strip() for item in args.variants.split(",") if item.strip()]
    unknown = set(variants) - set(LABELS)
    if unknown:
        raise SystemExit(f"unknown variants: {sorted(unknown)}")
    if not variants:
        raise SystemExit("no variant selected")
    if max(args.k) <= 0:
        raise SystemExit("k must be positive")

    # CPU thread count: set before loading torch models.
    import torch

    threads = int(os.environ.get("TORCH_THREADS", "0"))
    torch.set_num_threads(threads or (os.cpu_count() or 4))

    cases = load_cases(instances=args.instances, max_cases=args.max_cases)
    if not cases:
        raise SystemExit("no benchmark case found")
    print(
        f"cases={len(cases)} queries={sum(len(c.queries) for c in cases)} "
        f"embedder={args.embedder} variants={','.join(variants)} "
        f"k={args.k} k1={args.k1} k_rrf={args.k_rrf} threads={torch.get_num_threads()}"
    )

    embedder = Embedder(args.embedder)
    needs_reranker = any(v in ("mosaic", "reranker") for v in variants)
    reranker = Reranker() if needs_reranker else None

    max_k = max(args.k)
    rows: list[dict] = []
    started = time.time()

    for case in tqdm(cases, desc="domains"):
        services = list(case.services)
        endpoints = [endpoint for service in services for endpoint in service.endpoints]
        endpoint_ids = [endpoint.op_id for endpoint in endpoints]
        endpoint_service = np.asarray(
            [index for index, service in enumerate(services) for _ in service.endpoints],
            dtype=np.int64,
        )

        service_vectors = embedder.encode_passages([service.text for service in services])
        query_vectors = embedder.encode_queries([query.text for query in case.queries])
        endpoint_vectors = (
            embedder.encode_passages([endpoint.enriched_text for endpoint in endpoints])
            if "flat" in variants
            else None
        )

        for query_index, query in enumerate(case.queries):
            stage1_scores = service_vectors @ query_vectors[query_index]

            candidate_indices = np.zeros(0, dtype=np.int64)
            rerank_scores = np.zeros(0, dtype=np.float32)
            if needs_reranker:
                top_services = np.argsort(-stage1_scores, kind="stable")[: args.k1]
                candidate_indices = np.flatnonzero(
                    np.isin(endpoint_service, top_services)
                )
                rerank_scores = reranker.score(
                    query.text,
                    [endpoints[int(index)].enriched_text for index in candidate_indices],
                )

            rankings: dict[str, list[str]] = {}
            if "mosaic" in variants:
                rankings["mosaic"] = mosaic_rank(
                    stage1_scores=stage1_scores,
                    endpoint_ids=endpoint_ids,
                    endpoint_service=endpoint_service,
                    rerank_scores=rerank_scores,
                    candidate_indices=candidate_indices,
                    k1=args.k1,
                    k_rrf=args.k_rrf,
                    top_k=max_k,
                )
            if "reranker" in variants:
                rankings["reranker"] = reranker_rank(
                    endpoint_ids=endpoint_ids,
                    candidate_indices=candidate_indices,
                    rerank_scores=rerank_scores,
                    top_k=max_k,
                )
            if "flat" in variants:
                rankings["flat"] = flat_rank(
                    endpoint_ids=endpoint_ids,
                    endpoint_embeddings=endpoint_vectors,
                    query_embedding=query_vectors[query_index],
                    top_k=max_k,
                )

            for variant in variants:
                for k in args.k:
                    recall, precision, retrieved = _metrics(
                        rankings[variant], query.gold, k
                    )
                    rows.append(
                        {
                            "variant": variant,
                            "instance": case.instance,
                            "domain": case.domain,
                            "query_index": query_index,
                            "k": k,
                            "recall": recall,
                            "precision": precision,
                            "n_gold": len(query.gold),
                            "n_retrieved": retrieved,
                        }
                    )

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
    tag = args.tag or args.embedder
    frame.to_csv(args.out_dir / f"{tag}_per_query.csv", index=False)
    summary.to_csv(args.out_dir / f"{tag}_summary.csv", index=False)
    per_domain.to_csv(args.out_dir / f"{tag}_per_domain.csv", index=False)

    print(f"\nelapsed: {elapsed / 60:.1f} min")
    print("\n=== summary (mean over queries, %) ===")
    printable = summary.copy()
    printable["recall"] = (100 * printable["recall"]).round(1)
    printable["precision"] = (100 * printable["precision"]).round(1)
    print(printable.to_string(index=False))

    print("\n=== LaTeX row ===")
    label = f"MOSAIC (this work, {args.embedder}-small-en-v1.5)"
    if args.embedder == "qwen":
        label = "MOSAIC (this work, Qwen3-Embedding-0.6B)"
    for variant in variants:
        subset = summary[summary["variant"] == variant].set_index("k")
        cells: list[str] = []
        for k in args.k:
            cells.append(f"{100 * subset.loc[k, 'recall']:.1f}")
            cells.append(f"{100 * subset.loc[k, 'precision']:.1f}")
        print(f"{LABELS[variant]} & " + " & ".join(cells) + r" \\")

    if "mosaic" in variants:
        print("\n=== reference: Pesl et al., BGE-small, as reported (%) ===")
        for name, values in PESL_BGE_REFERENCE.items():
            cells = []
            for k in args.k:
                recall, precision = values[k]
                cells.extend([f"{recall:.1f}", f"{precision:.1f}"])
            print(f"{name:22s} & " + " & ".join(cells) + r" \\")

    print(f"\nfiles: {args.out_dir / (tag + '_per_query.csv')}")
    print(f"       {args.out_dir / (tag + '_summary.csv')}")
    print(f"       {args.out_dir / (tag + '_per_domain.csv')}")


if __name__ == "__main__":
    main()
