#!/usr/bin/env python3
"""Analyse the Designer evaluation run and produce paper-ready summaries."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from common import (
    CATEGORIES,
    PREDICTION_COLUMNS,
    catalog_op_keys,
    catalog_service_ids,
    contract_checks,
    dump_json,
    icc21,
    latest_run_dir,
    load_json,
    macro_f1,
    normalize_category,
    read_csv,
    weighted_kappa,
    wilson_ci,
    write_csv,
)


HERE = Path(__file__).resolve().parent
DEFAULT_RESULTS = HERE / "results"
PROPOSED_QUERIES = HERE / "queries" / "designer_queries.csv"
ADJUDICATED_QUERIES = HERE / "queries" / "designer_queries_adjudicated.csv"
DEFAULT_QUERIES = ADJUDICATED_QUERIES if ADJUDICATED_QUERIES.exists() else PROPOSED_QUERIES
DIAGNOSTIC_LABELS = list(CATEGORIES)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyse Designer evaluation results")
    parser.add_argument("--run-dir", default=None,
                        help="Run directory; default: latest results/run_*")
    parser.add_argument("--results-root", default=str(DEFAULT_RESULTS))
    parser.add_argument("--queries", default=str(DEFAULT_QUERIES),
                        help="Optional adjudicated query file used to override labels")
    parser.add_argument("--catalog", default=None,
                        help="Catalog snapshot; default: run manifest catalog or local file")
    parser.add_argument("--scores-a", default=None,
                        help="Rater A contract scores CSV")
    parser.add_argument("--scores-b", default=None,
                        help="Rater B contract scores CSV")
    parser.add_argument("--out-dir", default=None,
                        help="Output directory; default: run directory")
    return parser.parse_args()


def safe_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{100.0 * value:.1f}%"


def proportion_text(successes: int, total: int) -> str:
    low, high = wilson_ci(successes, total)
    return f"{successes}/{total} ({pct(successes / total if total else 0)}; 95% CI {pct(low)}–{pct(high)})"


def load_run_rows(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "per_query.csv"
    if not path.exists():
        raise FileNotFoundError(f"Missing {path}; run run_designer_eval.py first")
    return read_csv(path)


def merge_adjudicated_labels(rows: list[dict[str, Any]],
                             queries_path: Path) -> list[dict[str, Any]]:
    if not queries_path.exists():
        return rows
    labels = {row["query_id"]: row for row in read_csv(queries_path)}
    merged = []
    for row in rows:
        row = dict(row)
        label = labels.get(row.get("query_id", ""))
        if label:
            if label.get("gold_category"):
                row["gold_category"] = normalize_category(label["gold_category"])
            for key in ("subtype", "expected_behavior", "label_status", "notes"):
                if label.get(key):
                    row[key] = label[key]
        merged.append(row)
    return merged


def enrich(rows: list[dict[str, Any]], catalog_ids: set[str],
           catalog_ops: set[str]) -> list[dict[str, Any]]:
    enriched = []
    for row in rows:
        row = dict(row)
        gold = normalize_category(row.get("gold_category"))
        row["gold_category"] = gold
        network_error = bool(row.get("network_error"))
        predicted = normalize_category(row.get("predicted_category"))
        if network_error:
            predicted = "ERROR"
        elif not predicted:
            predicted = "NOT_REACHED"
        row["predicted_for_matrix"] = predicted
        row["reached_designer"] = int(
            str(row.get("reached_designer", "")).strip().lower()
            in ("1", "true", "yes")
        )
        row["tasks_before"] = safe_int(row.get("planner_tasks_count"))
        if row["tasks_before"] is None:
            row["tasks_before"] = safe_int(row.get("tasks_after_count"))
        row["tasks_after"] = safe_int(row.get("validated_tasks_count"))
        if row["tasks_after"] is None:
            row["tasks_after"] = safe_int(row.get("tasks_after_count"))

        contract = None
        if row.get("contract_json"):
            try:
                contract = json.loads(row["contract_json"])
            except json.JSONDecodeError:
                contract = None
        checks = contract_checks(contract, catalog_ids, catalog_ops)
        row["contract"] = contract
        row["contract_present"] = int(checks["contract_present"])
        row["contract_hard_valid"] = int(checks["contract_hard_valid"])
        row["contract_issues"] = " | ".join(checks["contract_issues"])
        row["contract_service_id"] = checks["contract_service_id"]
        row["contract_endpoint_count"] = checks["contract_endpoint_count"]
        row["reuse_exact_count"] = checks["reuse_exact_count"]
        row["reuse_exact_any"] = int(checks["reuse_exact_any"])
        row["reuse_endpoint_rate"] = checks["reuse_endpoint_rate"]
        row["duplicate_service_id"] = int(checks["duplicate_service_id"])
        enriched.append(row)
    return enriched


def confusion_matrix(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    matrix = Counter(
        (row.get("gold_category", ""), row.get("predicted_for_matrix", ""))
        for row in rows
        if row.get("set") == "diagnostic"
    )
    out = []
    for gold in DIAGNOSTIC_LABELS:
        record = {"gold": gold}
        for predicted in PREDICTION_COLUMNS:
            record[predicted] = matrix.get((gold, predicted), 0)
        record["total"] = sum(record[p] for p in PREDICTION_COLUMNS)
        out.append(record)
    return out


def diagnostic_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    diag = [row for row in rows if row.get("set") == "diagnostic"]
    total = len(diag)
    result: dict[str, Any] = {
        "n": total,
        "n_ood": sum(1 for row in diag if row.get("gold_category") == "OUT_OF_DOMAIN"),
        "by_category": {},
    }
    for category in CATEGORIES:
        subset = [row for row in diag if row.get("gold_category") == category]
        n = len(subset)
        reached = [row for row in subset if row.get("reached_designer")]
        correct = [row for row in reached if row.get("predicted_category") == category]
        nonempty_pre = [row for row in subset if (row.get("tasks_before") or 0) > 0]
        nonempty_post = [row for row in subset if (row.get("tasks_after") or 0) > 0]
        contracts = [row for row in subset if row.get("contract_present")]
        result["by_category"][category] = {
            "n": n,
            "reached": len(reached),
            "reach_rate": len(reached) / n if n else 0.0,
            "classification_correct": len(correct),
            "classification_accuracy_reached": len(correct) / len(reached) if reached else None,
            "end_to_end_correct": len(correct),
            "end_to_end_accuracy": len(correct) / n if n else 0.0,
            "nonempty_plan_pre": len(nonempty_pre),
            "silent_wrong_plan_pre": len(nonempty_pre),
            "silent_wrong_plan_pre_rate": len(nonempty_pre) / n if n else 0.0,
            "nonempty_plan_post": len(nonempty_post),
            "silent_wrong_plan_post": len(nonempty_post),
            "silent_wrong_plan_post_rate": len(nonempty_post) / n if n else 0.0,
            "contract_yield": len(contracts),
            "contract_yield_rate": len(contracts) / n if n else 0.0,
            "contract_hard_valid": sum(1 for row in contracts if row.get("contract_hard_valid")),
        }

    reached = [row for row in diag if row.get("reached_designer")]
    correct_reached = [
        row for row in reached
        if row.get("predicted_category") == row.get("gold_category")
    ]
    end_to_end_correct = [
        row for row in diag
        if row.get("reached_designer")
        and row.get("predicted_category") == row.get("gold_category")
    ]
    pre_nonempty = [row for row in diag if (row.get("tasks_before") or 0) > 0]
    post_nonempty = [row for row in diag if (row.get("tasks_after") or 0) > 0]
    result.update({
        "reached": len(reached),
        "reach_rate": len(reached) / total if total else 0.0,
        "classification_correct": len(correct_reached),
        "classification_accuracy_reached": (
            len(correct_reached) / len(reached) if reached else None
        ),
        "end_to_end_correct": len(end_to_end_correct),
        "end_to_end_accuracy": len(end_to_end_correct) / total if total else 0.0,
        "macro_f1_reached": macro_f1(
            [row["gold_category"] for row in reached],
            [row["predicted_category"] for row in reached],
            CATEGORIES,
        ) if reached else None,
        "macro_f1_end_to_end": macro_f1(
            [row["gold_category"] for row in diag],
            [row["predicted_for_matrix"] for row in diag],
            CATEGORIES,
        ) if diag else None,
        "nonempty_plan_pre": len(pre_nonempty),
        "silent_wrong_plan_pre_rate": len(pre_nonempty) / total if total else 0.0,
        "nonempty_plan_post": len(post_nonempty),
        "silent_wrong_plan_post_rate": len(post_nonempty) / total if total else 0.0,
        "not_reached": sum(1 for row in diag if not row.get("reached_designer")),
        "not_reached_rate": (
            sum(1 for row in diag if not row.get("reached_designer")) / total
            if total else 0.0
        ),
        "designer_unknown": sum(
            1 for row in diag if row.get("predicted_category") == "UNKNOWN"
        ),
        "contract_yield": sum(
            1 for row in diag
            if row.get("gold_category") == "OUT_OF_DOMAIN"
            and row.get("contract_present")
        ),
        "contract_yield_rate": (
            sum(
                1 for row in diag
                if row.get("gold_category") == "OUT_OF_DOMAIN"
                and row.get("contract_present")
            ) / 15
            if any(row.get("gold_category") == "OUT_OF_DOMAIN" for row in diag)
            else 0.0
        ),
        "contract_hard_valid": sum(
            1 for row in diag
            if row.get("gold_category") == "OUT_OF_DOMAIN"
            and row.get("contract_present")
            and row.get("contract_hard_valid")
        ),
        "contract_leakage": sum(
            1 for row in diag
            if row.get("gold_category") != "OUT_OF_DOMAIN" and row.get("contract_present")
        ),
        "confidence_intervals": {
            "reach": wilson_ci(len(reached), total),
            "classification_reached": wilson_ci(len(correct_reached), len(reached)),
            "end_to_end": wilson_ci(len(end_to_end_correct), total),
            "silent_wrong_pre": wilson_ci(len(pre_nonempty), total),
        },
    })
    return result


def smart_city_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    smart = [row for row in rows if row.get("set") == "smart_city"]
    total = len(smart)
    activated = [row for row in smart if row.get("reached_designer")]
    ood = [row for row in smart if row.get("predicted_category") == "OUT_OF_DOMAIN"]
    contracts = [row for row in smart if row.get("contract_present")]
    known_ret = [
        row for row in smart
        if str(row.get("ret_has_gold", "")) in ("0", "1")
    ]
    ret_has_gold = [
        row for row in known_ret if str(row.get("ret_has_gold")) == "1"
    ]
    fp_conditioned = [
        row for row in ret_has_gold if row.get("reached_designer")
    ]
    fp_due_to_retrieval = [
        row for row in activated if str(row.get("ret_has_gold", "")) == "0"
    ]
    return {
        "n": total,
        "designer_activation": len(activated),
        "designer_activation_rate": len(activated) / total if total else 0.0,
        "designer_activation_ci": wilson_ci(len(activated), total),
        "predicted_ood": len(ood),
        "predicted_ood_rate": len(ood) / total if total else 0.0,
        "contract_activation": len(contracts),
        "contract_activation_rate": len(contracts) / total if total else 0.0,
        "ret_has_gold_known": len(known_ret),
        "ret_has_gold": len(ret_has_gold),
        "ret_has_gold_rate": (
            len(ret_has_gold) / len(known_ret) if known_ret else None
        ),
        "fp_conditioned": len(fp_conditioned),
        "fp_conditioned_rate": (
            len(fp_conditioned) / len(ret_has_gold) if ret_has_gold else None
        ),
        "fp_due_to_retrieval": len(fp_due_to_retrieval),
        "fp_due_to_retrieval_rate": (
            len(fp_due_to_retrieval) / len(activated) if activated else None
        ),
    }


def abstention_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Treat Designer activation as a positive 'abstain' decision.

    The 45 diagnostic queries should abstain; the 100 smart-city queries should
    not.  This yields one precision/recall/F1 summary for the paper.
    """
    tp = sum(
        1 for row in rows
        if row.get("set") == "diagnostic" and row.get("reached_designer")
    )
    fn = sum(
        1 for row in rows
        if row.get("set") == "diagnostic" and not row.get("reached_designer")
    )
    fp = sum(
        1 for row in rows
        if row.get("set") == "smart_city" and row.get("reached_designer")
    )
    tn = sum(
        1 for row in rows
        if row.get("set") == "smart_city" and not row.get("reached_designer")
    )
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and precision + recall
        else None
    )
    return {
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": precision, "recall": recall, "f1": f1,
        "false_positive_rate_in_domain": fp / (fp + tn) if fp + tn else None,
    }


def route_breakdown(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts = Counter(
        (row.get("set", ""), row.get("gold_category", ""),
         row.get("route_reason", "") or "UNKNOWN")
        for row in rows
    )
    out = []
    for (set_name, gold, route), count in sorted(counts.items()):
        out.append({
            "set": set_name,
            "gold_category": gold,
            "route_reason": route,
            "count": count,
        })
    return out


def load_scores(path: Path | None) -> dict[str, dict[str, Any]]:
    if not path or not path.exists():
        return {}
    scores: dict[str, dict[str, Any]] = {}
    for row in read_csv(path):
        query_id = row.get("query_id", "")
        if not query_id:
            continue
        scores[query_id] = {
            "score": safe_int(row.get("score")),
            "reuse_semantic": (row.get("reuse_semantic") or "").strip().lower(),
            "notes": row.get("notes", ""),
        }
    return scores


def plausibility_metrics(rows: list[dict[str, Any]],
                         scores_a: dict[str, dict[str, Any]],
                         scores_b: dict[str, dict[str, Any]]) -> dict[str, Any]:
    ood = [
        row for row in rows
        if row.get("set") == "diagnostic"
        and row.get("gold_category") == "OUT_OF_DOMAIN"
    ]
    pairs: list[tuple[int, int]] = []
    query_means: list[float] = []
    semantic: list[bool] = []
    for row in ood:
        qid = row.get("query_id", "")
        a = scores_a.get(qid, {}).get("score")
        b = scores_b.get(qid, {}).get("score")
        if a is not None and b is not None:
            pairs.append((a, b))
            query_means.append((a + b) / 2.0)
        elif a is not None or b is not None:
            query_means.append(float(a if a is not None else b))
        for source in (scores_a.get(qid, {}), scores_b.get(qid, {})):
            value = source.get("reuse_semantic", "")
            if value in ("yes", "y", "true", "1"):
                semantic.append(True)
            elif value in ("no", "n", "false", "0"):
                semantic.append(False)
    scores = [score for pair in pairs for score in pair]
    result: dict[str, Any] = {
        "n_ood": len(ood),
        "n_scored_pairs": len(pairs),
        "n_scored_queries": len(query_means),
        "mean": statistics.mean(scores) if scores else None,
        "median": statistics.median(scores) if scores else None,
        "query_mean": statistics.mean(query_means) if query_means else None,
        "query_median": statistics.median(query_means) if query_means else None,
        "usable_ge_3": sum(1 for value in query_means if value >= 3),
        "usable_ge_3_rate": (
            sum(1 for value in query_means if value >= 3) / len(ood) if ood else None
        ),
        "exact_agreement": (
            sum(1 for a, b in pairs if a == b) / len(pairs) if pairs else None
        ),
        "within_one": (
            sum(1 for a, b in pairs if abs(a - b) <= 1) / len(pairs) if pairs else None
        ),
        "weighted_kappa": weighted_kappa(
            [a for a, _ in pairs], [b for _, b in pairs]
        ) if pairs else None,
        "icc21": icc21([(float(a), float(b)) for a, b in pairs]) if pairs else None,
        "semantic_reuse_yes": sum(1 for value in semantic if value),
        "semantic_reuse_no": sum(1 for value in semantic if not value),
        "semantic_reuse_rate": (
            sum(1 for value in semantic if value) / len(semantic) if semantic else None
        ),
    }
    return result


def reuse_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    contracts = [
        row for row in rows
        if row.get("set") == "diagnostic" and row.get("contract_present")
    ]
    exact = [row for row in contracts if row.get("reuse_exact_any")]
    dupes = [row for row in contracts if row.get("duplicate_service_id")]
    endpoint_rates = [
        float(row.get("reuse_endpoint_rate") or 0.0) for row in contracts
    ]
    return {
        "contracts": len(contracts),
        "exact_reuse_contracts": len(exact),
        "exact_reuse_rate": len(exact) / len(contracts) if contracts else None,
        "mean_reuse_endpoint_rate": (
            statistics.mean(endpoint_rates) if endpoint_rates else None
        ),
        "duplicate_service_id": len(dupes),
        "duplicate_service_id_rate": len(dupes) / len(contracts) if contracts else None,
    }


def sanitize_contract_for_review(contract: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(contract, dict):
        return None
    clean = dict(contract)
    # Hide chain-of-thought style rationale; the rater must judge the design,
    # not the persuasive text around it.
    clean.pop("rationale", None)
    return clean


def write_review_files(run_dir: Path, rows: list[dict[str, Any]]) -> None:
    ood = [
        row for row in rows
        if row.get("set") == "diagnostic"
        and row.get("gold_category") == "OUT_OF_DOMAIN"
        and row.get("contract_present")
    ]
    ood.sort(key=lambda row: row.get("query_id", ""))
    review_rows = []
    for index, row in enumerate(ood, 1):
        review_rows.append({
            "review_id": f"R{index:03d}",
            "query_id": row.get("query_id", ""),
            "query": row.get("query", ""),
            "contract_json": json.dumps(
                sanitize_contract_for_review(row.get("contract")),
                ensure_ascii=False,
            ),
            "score": "",
            "reuse_semantic": "",
            "notes": "",
        })
    fields = ["review_id", "query_id", "query", "contract_json", "score",
              "reuse_semantic", "notes"]
    write_csv(run_dir / "contracts_to_review_A.csv", review_rows, fields)
    write_csv(run_dir / "contracts_to_review_B.csv", review_rows, fields)


def summary_markdown(metrics: dict[str, Any]) -> str:
    diag = metrics["diagnostic"]
    smart = metrics["smart_city"]
    lines: list[str] = []
    lines.append("# Designer evaluation")
    lines.append("")
    lines.append(f"Run: `{metrics['run_dir']}`")
    lines.append(f"Queries: {diag['n']} diagnostic + {smart['n']} smart-city")
    lines.append("")
    lines.append("## Diagnostic set (45)")
    lines.append("")
    lines.append("| Classe | N | Reach | Class. acc. (reached) | End-to-end | Nonempty pre | Nonempty post | Contract yield |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for category in CATEGORIES:
        item = diag["by_category"][category]
        lines.append(
            f"| {category} | {item['n']} | {proportion_text(item['reached'], item['n'])} | "
            f"{pct(item['classification_accuracy_reached'])} | "
            f"{proportion_text(item['end_to_end_correct'], item['n'])} | "
            f"{proportion_text(item['nonempty_plan_pre'], item['n'])} | "
            f"{proportion_text(item['nonempty_plan_post'], item['n'])} | "
            f"{proportion_text(item['contract_yield'], item['n'])} |"
        )
    lines.append(
        f"| **Totale** | {diag['n']} | {proportion_text(diag['reached'], diag['n'])} | "
        f"{pct(diag['classification_accuracy_reached'])} | "
        f"{proportion_text(diag['end_to_end_correct'], diag['n'])} | "
        f"{proportion_text(diag['nonempty_plan_pre'], diag['n'])} | "
        f"{proportion_text(diag['nonempty_plan_post'], diag['n'])} | "
        f"{proportion_text(diag['contract_yield'], diag['n_ood'] or 1)} |"
    )
    lines.append("")
    lines.append("`Nonempty pre` = il planner ha prodotto almeno un task (il Designer non è stato raggiunto).")
    lines.append("`Nonempty post` = dopo validazione/auto-fix restano task eseguibili.")
    lines.append("`Contract yield` è calcolato sulle sole 15 query OOD.")
    lines.append("")
    lines.append("## Contract validity (OOD)")
    lines.append("")
    if diag["contract_yield"]:
        lines.append(f"- Contract hard-valid: {proportion_text(diag['contract_hard_valid'], diag['contract_yield'])} sui contratti prodotti")
    else:
        lines.append("- Contract hard-valid: n/a (nessun contratto prodotto)")
    if diag["n"] - diag["n_ood"]:
        lines.append(f"- Contract leakage su query non-OOD: {proportion_text(diag['contract_leakage'], diag['n'] - diag['n_ood'])}")
    else:
        lines.append("- Contract leakage su query non-OOD: n/a")
    lines.append("")
    lines.append("## Confusion matrix")
    lines.append("")
    header = "| gold \\ pred | " + " | ".join(PREDICTION_COLUMNS) + " |"
    lines.append(header)
    lines.append("|---|" + "---:|" * len(PREDICTION_COLUMNS))
    for row in metrics["confusion_matrix"]:
        values = " | ".join(str(row[column]) for column in PREDICTION_COLUMNS)
        lines.append(f"| {row['gold']} | {values} |")
    lines.append("")
    lines.append("## Smart-city 100 (in-domain)")
    lines.append("")
    lines.append("| Metrica | Valore |")
    lines.append("|---|---:|")
    lines.append(f"| Designer activation (FP route) | {proportion_text(smart['designer_activation'], smart['n'])} |")
    lines.append(f"| Designer says OUT_OF_DOMAIN | {proportion_text(smart['predicted_ood'], smart['n'])} |")
    lines.append(f"| Contract emitted | {proportion_text(smart['contract_activation'], smart['n'])} |")
    if smart["ret_has_gold_known"]:
        lines.append(
            f"| Gold op(s) in Ret | {proportion_text(smart['ret_has_gold'], smart['ret_has_gold_known'])} |"
        )
        if smart["ret_has_gold"]:
            lines.append(
                f"| FP | gold op in Ret | {proportion_text(smart['fp_conditioned'], smart['ret_has_gold'])} |"
            )
    if smart["designer_activation"]:
        lines.append(
            f"| FP attributable to retrieval | {proportion_text(smart['fp_due_to_retrieval'], smart['designer_activation'])} |"
        )
    lines.append("")
    lines.append("## Abstention (45 vs 100)")
    lines.append("")
    abst = metrics["abstention"]
    lines.append(
        f"TP={abst['tp']} FP={abst['fp']} FN={abst['fn']} TN={abst['tn']} · "
        f"precision={pct(abst['precision'])} · recall={pct(abst['recall'])} · F1={pct(abst['f1'])}"
    )
    lines.append("")
    lines.append("## Contract plausibility")
    lines.append("")
    plaus = metrics["plausibility"]
    if plaus["n_scored_pairs"]:
        lines.append(f"- Coppie valutate: {plaus['n_scored_pairs']}")
        lines.append(f"- Media (tutti i voti): {plaus['mean']:.2f}")
        lines.append(f"- Mediana (tutti i voti): {plaus['median']:.2f}")
        lines.append(f"- Media per query: {plaus['query_mean']:.2f}")
        lines.append(f"- Contract usabili (media >= 3): {proportion_text(plaus['usable_ge_3'], plaus['n_ood'])}")
        lines.append(f"- Accordo esatto: {pct(plaus['exact_agreement'])}")
        lines.append(f"- Entro 1 punto: {pct(plaus['within_one'])}")
        lines.append(f"- Weighted kappa: {plaus['weighted_kappa']:.3f}" if plaus['weighted_kappa'] is not None else "- Weighted kappa: n/a")
        lines.append(f"- ICC(2,1): {plaus['icc21']:.3f}" if plaus['icc21'] is not None else "- ICC(2,1): n/a")
        semantic_total = plaus['semantic_reuse_yes'] + plaus['semantic_reuse_no']
        lines.append(
            f"- Riuso semantico dichiarato: {proportion_text(plaus['semantic_reuse_yes'], semantic_total or 1)}"
        )
    else:
        lines.append("Nessun punteggio disponibile. Compila `contracts_to_review_A.csv` e `_B.csv`.")
    lines.append("")
    lines.append("## Reuse")
    lines.append("")
    reuse = metrics["reuse"]
    lines.append(f"- Contract totali: {reuse['contracts']}")
    lines.append(f"- Contract con almeno un endpoint già nel catalogo: {proportion_text(reuse['exact_reuse_contracts'], reuse['contracts'])}")
    if reuse["mean_reuse_endpoint_rate"] is not None:
        lines.append(f"- Percentuale media di endpoint riusati: {pct(reuse['mean_reuse_endpoint_rate'])}")
    lines.append(f"- service_id duplicati: {proportion_text(reuse['duplicate_service_id'], reuse['contracts'])}")
    lines.append("")
    lines.append("## Failure breakdown")
    lines.append("")
    lines.append("| Set | Gold | Route reason | Count |")
    lines.append("|---|---|---|---:|")
    for row in metrics["route_breakdown"]:
        lines.append(f"| {row['set']} | {row['gold_category']} | {row['route_reason']} | {row['count']} |")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    run_dir = Path(args.run_dir) if args.run_dir else latest_run_dir(args.results_root)
    out_dir = Path(args.out_dir) if args.out_dir else run_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = load_run_rows(run_dir)
    rows = merge_adjudicated_labels(rows, Path(args.queries))

    if any(
        row.get("set") == "diagnostic"
        and (row.get("label_status") or "").lower() != "adjudicated"
        for row in rows
    ):
        print(
            "[WARN] Il file query non è adjudicated: i risultati sono provvisori.",
            file=sys.stderr,
        )

    catalog_path = None
    if args.catalog:
        catalog_path = Path(args.catalog)
    else:
        manifest_path = run_dir / "run_manifest.json"
        if manifest_path.exists():
            manifest = load_json(manifest_path)
            if manifest.get("catalog"):
                catalog_path = Path(manifest["catalog"])
        if not catalog_path or not catalog_path.exists():
            local = HERE / "catalog_snapshot.json"
            if local.exists():
                catalog_path = local
    catalog_payload = None
    catalog_ids: set[str] = set()
    catalog_ops: set[str] = set()
    if catalog_path and catalog_path.exists():
        catalog_payload = load_json(catalog_path)
        catalog_ids = catalog_service_ids(catalog_payload)
        catalog_ops = catalog_op_keys(catalog_payload)
    else:
        # Fallback: use the union of the retrieved capabilities as a proxy.
        for row in rows:
            try:
                discovered = json.loads(row.get("discovered_json") or "[]")
            except json.JSONDecodeError:
                discovered = []
            for service in discovered:
                caps = service.get("capabilities") or []
                if isinstance(caps, list):
                    for cap in caps:
                        from Eval.designer_eval.common import endpoint_key, parse_endpoint_key
                        parsed = parse_endpoint_key(cap)
                        if parsed:
                            catalog_ops.add(endpoint_key(*parsed))
    rows = enrich(rows, catalog_ids, catalog_ops)

    scores_a_path = Path(args.scores_a) if args.scores_a else run_dir / "contract_scores_A.csv"
    scores_b_path = Path(args.scores_b) if args.scores_b else run_dir / "contract_scores_B.csv"
    scores_a = load_scores(scores_a_path)
    scores_b = load_scores(scores_b_path)

    metrics = {
        "run_dir": str(run_dir.resolve()),
        "catalog": str(catalog_path.resolve()) if catalog_path and catalog_path.exists() else None,
        "diagnostic": diagnostic_metrics(rows),
        "smart_city": smart_city_metrics(rows),
        "abstention": abstention_metrics(rows),
        "plausibility": plausibility_metrics(rows, scores_a, scores_b),
        "reuse": reuse_metrics(rows),
        "confusion_matrix": confusion_matrix(rows),
        "route_breakdown": route_breakdown(rows),
        "scores_files": {
            "A": str(scores_a_path),
            "B": str(scores_b_path),
        },
    }

    write_review_files(run_dir, rows)
    dump_json(out_dir / "summary.json", metrics)
    write_csv(
        out_dir / "confusion_matrix.csv",
        metrics["confusion_matrix"],
        ["gold", *PREDICTION_COLUMNS, "total"],
    )
    write_csv(
        out_dir / "failure_breakdown.csv",
        metrics["route_breakdown"],
        ["set", "gold_category", "route_reason", "count"],
    )
    enriched_fields = [
        "set", "query_id", "query", "gold_category", "predicted_category",
        "predicted_for_matrix", "reached_designer", "route_reason",
        "planner_status", "tasks_before", "tasks_after", "contract_present",
        "contract_hard_valid", "contract_issues", "reuse_exact_any",
        "reuse_exact_count", "reuse_endpoint_rate", "duplicate_service_id",
        "ret_has_gold", "error",
    ]
    write_csv(out_dir / "per_query_enriched.csv", rows, enriched_fields)
    (out_dir / "summary.md").write_text(
        summary_markdown(metrics), encoding="utf-8"
    )

    print(f"Analisi scritta in: {out_dir}")
    print(f"  summary.md:          {out_dir / 'summary.md'}")
    print(f"  summary.json:        {out_dir / 'summary.json'}")
    print(f"  confusion_matrix:    {out_dir / 'confusion_matrix.csv'}")
    print(f"  failure_breakdown:   {out_dir / 'failure_breakdown.csv'}")
    print(f"  contratti da votare: {run_dir / 'contracts_to_review_A.csv'}")
    print(f"  contratti da votare: {run_dir / 'contracts_to_review_B.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
