"""Endpoint ranking strategies.

``mosaic`` is the system under test. Given a query:

  1. stage 1 ranks the *services* by cosine similarity on their description;
  2. stage 2 scores every endpoint of the top-K1 services with the cross-encoder;
  3. equation (1) fuses the two rankings at endpoint level:
         F(o) = 1 / (K_RRF + rank_1(service(o))) + 1 / (K_RRF + rank_2(o))
     where rank_1 is the stage-1 rank of the owning service and rank_2 is the
     cross-encoder rank of the endpoint.

This is exactly equation (1) of the paper, which is defined per operation:
rho_1(o) is the rank of the service declaring o, rho_2(o) is the rank of o in
the second stage. The public db-gateway implementation computes the fusion at
service level (using each service's best endpoint score); the harness follows
the paper's per-operation definition, because the metric here is endpoint-level.

Two ablations are provided for diagnosis, not for the paper table:
  * ``reranker`` -- cross-encoder score only (no RRF, no stage-1 signal);
  * ``flat``     -- one bi-encoder over the same enriched endpoint texts.

Duplicate operation ids (the same ``METHOD /path`` exposed by two services in
the same domain) are collapsed to one id, keeping the best score, as Pesl et
al. do in ``_get_endpoints_from``.
"""

from __future__ import annotations

import numpy as np


def _best_by_id(ids: list[str], scores: np.ndarray) -> dict[str, float]:
    best: dict[str, float] = {}
    for op_id, score in zip(ids, scores):
        value = float(score)
        previous = best.get(op_id)
        if previous is None or value > previous:
            best[op_id] = value
    return best


def _sort_best(best: dict[str, float], top_k: int) -> list[str]:
    ordered = sorted(best.items(), key=lambda item: (-item[1], item[0]))
    return [op_id for op_id, _ in ordered[:top_k]]


def mosaic_rank(
    stage1_scores: np.ndarray,
    endpoint_ids: list[str],
    endpoint_service: np.ndarray,
    rerank_scores: np.ndarray,
    candidate_indices: np.ndarray,
    k1: int,
    k_rrf: int,
    top_k: int,
) -> list[str]:
    service_order = np.argsort(-stage1_scores, kind="stable")
    selected = service_order[:k1]
    service_rank = {int(service): rank + 1 for rank, service in enumerate(service_order)}

    endpoint_order = np.argsort(-rerank_scores, kind="stable")
    endpoint_rank = {
        int(candidate_indices[position]): rank + 1
        for rank, position in enumerate(endpoint_order)
    }

    fused: dict[str, tuple[float, float]] = {}
    for position, endpoint_index in enumerate(candidate_indices):
        endpoint_index = int(endpoint_index)
        service = int(endpoint_service[endpoint_index])
        score = float(rerank_scores[position])
        value = (
            1.0 / (k_rrf + service_rank[service])
            + 1.0 / (k_rrf + endpoint_rank[endpoint_index])
        )
        op_id = endpoint_ids[endpoint_index]
        previous = fused.get(op_id)
        if previous is None or (value, score) > previous:
            fused[op_id] = (value, score)

    ordered = sorted(fused.items(), key=lambda item: (-item[1][0], -item[1][1], item[0]))
    return [op_id for op_id, _ in ordered[:top_k]]


def reranker_rank(
    endpoint_ids: list[str],
    candidate_indices: np.ndarray,
    rerank_scores: np.ndarray,
    top_k: int,
) -> list[str]:
    best = _best_by_id(
        [endpoint_ids[int(index)] for index in candidate_indices], rerank_scores
    )
    return _sort_best(best, top_k)


def flat_rank(
    endpoint_ids: list[str],
    endpoint_embeddings: np.ndarray,
    query_embedding: np.ndarray,
    top_k: int,
) -> list[str]:
    similarities = endpoint_embeddings @ query_embedding
    return _sort_best(_best_by_id(endpoint_ids, similarities), top_k)
