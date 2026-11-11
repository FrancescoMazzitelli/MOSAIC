#!/usr/bin/env python3
"""
Test Runner — Dataset-based evaluation with RestGPT metrics.

This module loads the benchmark datasets, sends the queries
to the Control Unit and computes the evaluation metrics defined in the RestGPT paper.

Computed metrics:
    - Success Rate (S%)        : Percentage of queries in which all tasks succeed.
    - Correct Path Rate (CP%)  : Percentage of queries in which the task sequence (execution plan)
                                 matches the oracle exactly.
    - Delta Solution Length    : Mean absolute difference between the length of the generated plan
      (ΔSL)                      and that of the oracle (|planned| - |oracle|).
    - Latency (sec)            : Execution time of the planning phase.

Expected/generated directory structure:
    results/
      test_runs/
        <dataset>_<model>_<run_id>/
          responses.json       — Full responses for each query (JSON).
          metrics.json         — Dataset-level aggregate metrics (JSON).
          summary.txt          — Human-readable summary report (Text).
          per_query.csv        — Tabular detail of per-query metrics (CSV).

Usage examples:
    # Run against the running Control Unit (default http://localhost:5500):
    python benchmark/test_runner.py

    # Select specific datasets and the control-unit URL:
    python benchmark/test_runner.py --datasets tmdb spotify --control-url http://localhost:5500

    # Select a custom output directory:
    python benchmark/test_runner.py --output-dir results/my_run
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATASETS_DIR = PROJECT_ROOT / "datasets"
RESULTS_DIR  = PROJECT_ROOT / "results" / "test_runs"

BACKEND_TYPES = {
    "ollama": "Ollama",
    "llamacpp": "llama.cpp",
}


def parse_args():
    """Parse the command-line arguments.

    Returns:
        argparse.Namespace: An object holding the parsed arguments.
    """
    p = argparse.ArgumentParser(description="Test Runner — dataset-based evaluation with RestGpt metrics")
    p.add_argument(
        "--datasets", nargs="+", default=None,
        help="Dataset(s) to evaluate (default: all JSON files in datasets/)"
    )
    p.add_argument(
        "--control-url", default="http://localhost:5500",
        help="Base URL of the Control Unit (default: http://localhost:5500)"
    )
    p.add_argument(
        "--output-dir", default=None,
        help="Directory di output (default: results/test_runs/<dataset>_<model>_<timestamp>)"
    )
    p.add_argument(
        "--model", default=None,
        help="Model identifier used to name the directory (default: from the LLM_MODEL env var or 'unknown')"
    )
    p.add_argument(
        "--run-id", default=None,
        help="Custom run identifier (default: auto-generated timestamp)"
    )
    p.add_argument(
        "--max-queries", type=int, default=None,
        help="Maximum number of queries per dataset (useful for quick smoke tests)"
    )
    return p.parse_args()


def discover_datasets(selected=None):
    """Load the JSON datasets from the dedicated directory into memory.

    Args:
        selected (list[str], optional): List of dataset names to include (without the .json extension).
            If None, all available datasets are loaded.

    Returns:
        dict: A dictionary whose keys are the dataset names and whose values are
            the lists of queries/oracles loaded from the JSON.
    """
    datasets = {}
    for fpath in sorted(DATASETS_DIR.glob("*.json")):
        stem = fpath.stem
        if selected and stem not in selected:
            continue
        with open(fpath, "r", encoding="utf-8") as f:
            datasets[stem] = json.load(f)
    return datasets


def invoke_control_unit(query, control_url):
    """Send a single query to the Control Unit via a POST request.

    Args:
        query (str): The query text to submit in natural language.
        control_url (str): The base URL of the Control Unit.

    Returns:
        tuple: A tuple containing:
            - dict: Response data as JSON. If an error occurs,
              it contains the error details.
            - float: Elapsed time (latency) in seconds.
            - int: HTTP status code of the response.
    """
    payload = {"input": query}
    t0 = time.perf_counter()
    resp = requests.post(
        f"{control_url}/api/control/invoke",
        json=payload,
        timeout=None,
    )
    elapsed = time.perf_counter() - t0
    http_status = resp.status_code
    try:
        data = resp.json()
    except Exception:
        data = {"error": f"non-json response (HTTP {http_status})", "raw_text": resp.text[:500]}
    return data, elapsed, http_status


# =====================================================================
# PATH / STEP MATCHING SECTION
# =====================================================================
# This section implements the comparison between the generated execution plan
# and the oracle, with three corrections (FIX) over the base standard:
#
#   FIX 1 (Placeholder): Masks placeholders (e.g. {{...}}) before parsing
#         the URL (urlparse), preventing an embedded JMESPath filter
#         such as {{t.items[?name=='X'] | [0].id}} from being split at the '?'.
#
#   FIX 2 (Service Suffix): Explicitly removes known REST prefixes
#         (e.g. /rest/<svc>/<ver>), ensuring an exact comparison of the number of
#         path segments to avoid false positives.
#
#   FIX 3 (DAG Linear Extension): Also treats as a valid "Correct Path"
#         a legitimate linear extension of the oracle's DAG (Directed Acyclic Graph).
#         Independent GET requests (which do not depend on IDs from previous
#         tasks) can be reordered without invalidating the plan.
#
# NOTE: Redundant intermediate GETs are not excused
#       by this logic and must be analysed separately.
# =====================================================================

_PLACEHOLDER_RE    = re.compile(r"\{\{.*?\}\}")          # Match placeholders, non-greedy
_SERVICE_PREFIX_RE = re.compile(r"^/rest/[^/]+/[^/]+")   # Match prefixes such as /rest/Spotify/1.0

# Path parameters considered "LITERAL" (e.g. {type}, {region}). They generate no
# temporal or ordering dependencies between tasks. Anything not listed here is treated as dependent.
_LITERAL_PATH_PARAMS = {"type", "media_type", "time_window", "language", "region", "sort_by"}


def _is_literal_param(name, literal=frozenset()):
    """Check whether a parameter belongs to a defined literal set or is a number (enum)."""
    return name in _LITERAL_PATH_PARAMS or name in literal or name.endswith("_number")


def derive_literal_path_params(spec):
    """Extract the names of path parameters defined as 'enum' in the OpenAPI specs.

    Args:
        spec (dict): The OpenAPI specification of the service.

    Returns:
        set: A set of path parameter names that have predefined (literal) values
             and do not depend on IDs generated by previous calls.
    """
    out = set()
    for _p, item in (spec.get("paths") or {}).items():
        for _m, op in (item or {}).items():
            if not isinstance(op, dict):
                continue
            for prm in op.get("parameters", []) or []:
                if prm.get("in") == "path" and (prm.get("schema") or {}).get("enum"):
                    out.add(prm.get("name"))
    return out


def _clean_path(url):
    """Clean and normalise the URL of a generated task for comparison (applies FIX 1 and FIX 2)."""
    if not url:
        return ""
    masked = _PLACEHOLDER_RE.sub("{ph}", url)            # FIX 1
    parsed = urlparse(masked)
    path = parsed.path.rstrip("/")
    if not path and " " in url:
        path = url.split(" ")[-1].rstrip("/")
    path = _SERVICE_PREFIX_RE.sub("", path)              # FIX 2
    return path


def extract_operation_path(task):
    """Extract the HTTP method and the normalised path from a single plan task."""
    operation = task.get("operation", "GET").upper()
    url = task.get("url") or task.get("endpoint") or ""
    return operation, _clean_path(url)


def normalize_oracle_path(path):
    """Remove whitespace and trailing slashes from an oracle path."""
    return path.strip().rstrip("/")


def _is_wildcard_segment(segment):
    """Determine whether the URL segment is a wildcard or a dynamic placeholder."""
    return bool(re.fullmatch(r"\{[^/{}]+\}|\{ph\}", segment)) or segment.startswith("{{")


def _segments_match(planned_seg, oracle_seg):
    """Check the match between a single generated segment and one from the oracle."""
    if _is_wildcard_segment(oracle_seg) or _is_wildcard_segment(planned_seg):
        return True
    return planned_seg == oracle_seg


def _step_matches(planned_step, oracle_step):
    """Compare an entire generated step with one from the oracle.

    Args:
        planned_step (tuple): (HTTP_Method, Clean_Path) tuple of the generated task.
        oracle_step (tuple): (HTTP_Method, Oracle_Path) tuple.

    Returns:
        bool: True if they match exactly by method and segment logic, False otherwise.
    """
    p_op, p_path = planned_step
    o_op, o_path = oracle_step
    if p_op != o_op:
        return False
    p_seg = [s for s in p_path.split("/") if s]
    o_seg = [s for s in o_path.split("/") if s]
    if len(p_seg) != len(o_seg):                         # FIX 2: Prevents improper fallbacks
        return False
    return all(_segments_match(ps, os) for ps, os in zip(p_seg, o_seg))


def _is_source(op, path, literal=frozenset()):
    """Check whether a task represents an independent SOURCE (it needs no previous outputs)."""
    if op != "GET":
        return False
    for seg in path.split("/"):
        mm = re.fullmatch(r"\{([^/{}]+)\}", seg)
        if mm and not _is_literal_param(mm.group(1), literal):
            return False
    return True


def _build_partial_order(oracle, literal=frozenset()):
    """Build the ordering constraints (DAG) from the oracle sequence.

    Implemented rules:
    - SOURCE -> DEPENDENT: A dependency must always follow its source.
    - DEPENDENT -> DEPENDENT: The order among dependencies must remain fixed.
    SOURCE steps have no constraints among themselves and may vary in order.

    Returns:
        set: A set of tuples (i, j) indicating the constraints (task `i` must precede `j`).
    """
    is_src = [_is_source(op, p, literal) for (op, p) in oracle]
    edges = set()
    for i, src in enumerate(is_src):
        if src:
            for j in range(i + 1, len(oracle)):
                if not is_src[j]:
                    edges.add((i, j))
    deps = [i for i, s in enumerate(is_src) if not s]
    for a, b in zip(deps, deps[1:]):
        edges.add((a, b))
    return edges


def _assign_respecting_order(planned_steps, oracle_norm, literal=frozenset()):
    """Attempt a valid injective matching (assignment) that respects the ordering constraints.

    Returns:
        dict | None: A dictionary {oracle_index: planned_index} if the plan
                     is a valid linear extension of the DAG. Otherwise, None.
    """
    cand = []
    for o in oracle_norm:
        c = [pi for pi, p in enumerate(planned_steps) if _step_matches(p, o)]
        if not c:
            return None
        cand.append(c)
    edges = _build_partial_order(oracle_norm, literal)
    used, assign = set(), {}

    def bt(k):
        if k == len(oracle_norm):
            return True
        for pi in cand[k]:
            if pi in used:
                continue
            ok = True
            for (i, j) in edges:
                if i == k and j in assign and pi > assign[j]:
                    ok = False; break
                if j == k and i in assign and assign[i] > pi:
                    ok = False; break
            if not ok:
                continue
            used.add(pi); assign[k] = pi
            if bt(k + 1):
                return True
            used.discard(pi); del assign[k]
        return False

    return dict(assign) if bt(0) else None


def compare_plan_with_oracle(tasks, oracle_steps, backend_mode="MOCK", literal=frozenset()):
    """Perform an in-depth analytical comparison between the generated plan and the oracle steps.

    Args:
        tasks (list[dict]): List of tasks extracted from the generated execution plan.
        oracle_steps (list[str]): The tasks defined as the correct solution.
        backend_mode (str): Usage mode (currently mock/default).
        literal (frozenset): Set of input parameters recognised as constants.

    Returns:
        dict: Dictionary containing matches, mismatches, F1 metrics and the `is_correct_path` boolean.
    """
    planned_steps = [extract_operation_path(t) for t in tasks]

    oracle_normalized = []
    for step in oracle_steps:
        parts = step.strip().split(maxsplit=1)
        if len(parts) == 2:
            oracle_normalized.append((parts[0].strip().upper(),
                                      normalize_oracle_path(parts[1])))

    # Check that the path is correct while respecting the DAG
    assignment = _assign_respecting_order(planned_steps, oracle_normalized, literal)
    is_correct_path = assignment is not None

    if assignment is not None:
        matched_idx = set(assignment.values())
        matches = [{"planned": planned_steps[assignment[i]], "oracle": o}
                   for i, o in enumerate(oracle_normalized)]
        oracle_missed = []
    else:
        matched_idx, matches, oracle_missed = set(), [], []
        for o in oracle_normalized:
            hit = next((pi for pi, p in enumerate(planned_steps)
                        if pi not in matched_idx and _step_matches(p, o)), None)
            if hit is None:
                oracle_missed.append({"oracle": o, "planned": None})
            else:
                matched_idx.add(hit)
                matches.append({"planned": planned_steps[hit], "oracle": o})
    mismatches = [{"planned": p, "oracle": None}
                  for pi, p in enumerate(planned_steps) if pi not in matched_idx]

    # Diagnostics (Recall/Precision/F1), order-independent
    used, step_hits, step_missed = set(), 0, []
    for o in oracle_normalized:
        hit = next((pi for pi, p in enumerate(planned_steps)
                    if pi not in used and _step_matches(p, o)), None)
        if hit is None:
            step_missed.append({"oracle": o, "planned": None})
        else:
            used.add(hit); step_hits += 1

    num_planned, num_oracle = len(planned_steps), len(oracle_normalized)
    step_recall = 1.0 if num_oracle == 0 else step_hits / num_oracle
    step_precision = 0.0 if num_planned == 0 else step_hits / num_planned
    step_f1 = (0.0 if step_precision + step_recall == 0
               else 2 * step_precision * step_recall / (step_precision + step_recall))

    return {
        "planned_steps": planned_steps,
        "oracle_steps": oracle_normalized,
        "matches": matches,
        "mismatches": mismatches,
        "oracle_missed": oracle_missed,
        "is_correct_path": is_correct_path,
        "num_planned": num_planned,
        "num_oracle": num_oracle,
        "step_hits": step_hits,
        "step_missed": step_missed,
        "step_recall": round(step_recall, 4),
        "step_precision": round(step_precision, 4),
        "step_f1": round(step_f1, 4),
    }


def is_all_successful(execution_results):
    """Check whether all tasks in the results reported status 'SUCCESS'."""
    for r in execution_results:
        if r.get("status") != "SUCCESS":
            return False
    return True


def compute_delta_sl(planned_count, oracle_count):
    """Compute the absolute Delta Solution Length (|planned| - |oracle|)."""
    return abs(planned_count - oracle_count)


def evaluate_dataset(dataset_name, queries, control_url, max_queries=None, output_dir=None):
    """Orchestrate the evaluation of all queries in a specific dataset.

    Args:
        dataset_name (str): Name of the dataset being run.
        queries (list[dict]): List of queries in the dataset.
        control_url (str): Endpoint to call for plan inference.
        max_queries (int, optional): Limit execution to the first N results.
        output_dir (Path, optional): Folder where temporary progress is saved.

    Returns:
        list[dict]: Array with the detailed results for each submitted query.
    """
    if max_queries:
        queries = queries[:max_queries]

    total = len(queries)
    results = []

    for idx, entry in enumerate(queries):
        query = entry["query"]
        oracle = entry.get("solution", [])

        print(f"[{idx+1}/{total}] {dataset_name}: {query[:80]}{'...' if len(query) > 80 else ''}")

        try:
            response_data, total_latency, http_status = invoke_control_unit(query, control_url)
        except requests.exceptions.RequestException as e:
            print(f"  ERROR: HTTP request failed — {e}")
            response_data = {"error": str(e)}
            total_latency = 0.0
            http_status = 0

        execution_plan = response_data.get("execution_plan", {})
        execution_results = response_data.get("execution_results", [])
        planning_latency = response_data.get("planning_latency_s", 0.0) or 0.0
        error = response_data.get("error", "")

        tasks = execution_plan.get("tasks", [])
        comparison = compare_plan_with_oracle(tasks, oracle)

        tasks_successful = is_all_successful(execution_results) if execution_results else False
        delta_sl = compute_delta_sl(comparison["num_planned"], comparison["num_oracle"])

        per_query = {
            "query_index": idx + 1,
            "query": query,
            "oracle": oracle,
            "error": error,
            "http_status": http_status,
            "planning_latency_s": round(planning_latency, 3),
            "total_latency_s": round(total_latency, 3),
            "planned_count": comparison["num_planned"],
            "oracle_count": comparison["num_oracle"],
            "delta_sl": delta_sl,
            "is_correct_path": comparison["is_correct_path"],
            "step_recall": comparison["step_recall"],
            "step_precision": comparison["step_precision"],
            "step_f1": comparison["step_f1"],
            "step_missed": comparison["step_missed"],
            "tasks_successful": tasks_successful,
            "matches": comparison["matches"],
            "mismatches": comparison["mismatches"],
            "oracle_missed": comparison["oracle_missed"],
            "execution_plan": execution_plan,
            "execution_results": [
                {
                    "task_name": r.get("task_name"),
                    "operation": r.get("operation"),
                    "status": r.get("status"),
                    "status_code": r.get("status_code"),
                }
                for r in (execution_results or [])
            ],
        }
        results.append(per_query)

        status = "OK" if comparison["is_correct_path"] and tasks_successful else "MISMATCH"
        print(f"  → {status}  planned={comparison['num_planned']} oracle={comparison['num_oracle']} "
              f"ΔSL={delta_sl} stepF1={comparison['step_f1']:.2f} latency={planning_latency:.2f}s")

        if output_dir:
            _save_incremental(results, dataset_name, output_dir)

    return results


def _save_incremental(results, dataset_name, output_dir):
    """Save the results to disk in a temporary format, allowing resilience.

    Each iteration atomically updates (via replace):
      - responses.json    : Contains the full payload (request/response/metrics).
      - per_query.csv     : Summary tabular metrics.
      - metrics.json      : Updated calculations and aggregations.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    tmp = output_dir / ".responses.tmp"
    final = output_dir / "responses.json"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    tmp.replace(final)

    tmp = output_dir / ".per_query.tmp"
    final = output_dir / "per_query.csv"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "query_index", "query", "oracle_count", "planned_count",
            "delta_sl", "is_correct_path", "step_recall", "step_precision",
            "step_f1", "tasks_successful",
            "planning_latency_s", "total_latency_s", "http_status", "error",
        ])
        for r in results:
            writer.writerow([
                r["query_index"], r["query"], r["oracle_count"],
                r["planned_count"], r["delta_sl"], r["is_correct_path"],
                r.get("step_recall", ""), r.get("step_precision", ""),
                r.get("step_f1", ""), r["tasks_successful"], r["planning_latency_s"],
                r["total_latency_s"], r.get("http_status", ""), r.get("error", ""),
            ])
    tmp.replace(final)

    metrics = compute_aggregate_metrics(results, dataset_name)
    tmp = output_dir / ".metrics.tmp"
    final = output_dir / "metrics.json"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2, ensure_ascii=False)
    tmp.replace(final)


def compute_aggregate_metrics(results, dataset_name):
    """Compute the mean and aggregate metrics (RestGPT) from the array of individual results.

    Args:
        results (list[dict]): List produced by `evaluate_dataset`.
        dataset_name (str): Name associated with the aggregation (e.g. 'tmdb').

    Returns:
        dict: Summary dictionary containing P50, P95 and mean for Recall, Precision, Latency.
    """
    total = len(results)
    if total == 0:
        return {
            "dataset": dataset_name,
            "total_queries": 0,
            "success_rate": 0.0,
            "correct_path_rate": 0.0,
            "avg_delta_sl": 0.0,
            "avg_step_recall": 0.0,
            "avg_step_precision": 0.0,
            "avg_step_f1": 0.0,
            "avg_planning_latency_s": 0.0,
            "avg_total_latency_s": 0.0,
            "p50_planning_latency_s": 0.0,
            "p95_planning_latency_s": 0.0,
        }

    successful = sum(1 for r in results if r["tasks_successful"])
    correct_path = sum(1 for r in results if r["is_correct_path"])
    total_delta_sl = sum(r["delta_sl"] for r in results)
    total_step_recall = sum(r.get("step_recall", 0.0) for r in results)
    total_step_precision = sum(r.get("step_precision", 0.0) for r in results)
    total_step_f1 = sum(r.get("step_f1", 0.0) for r in results)
    planning_latencies = [r["planning_latency_s"] for r in results]
    total_latencies = [r["total_latency_s"] for r in results]
    sorted_planning = sorted(planning_latencies)
    sorted_total = sorted(total_latencies)

    n = len(sorted_planning)
    p50_planning = sorted_planning[n // 2]
    p95_idx = min(int(n * 0.95), n - 1)
    p95_planning = sorted_planning[p95_idx]

    # Compute the HTTP status code distribution
    status_counts = {}
    http_errors = 0
    for r in results:
        s = r.get("http_status", 0)
        status_counts[s] = status_counts.get(s, 0) + 1
        if s and s >= 400:
            http_errors += 1

    return {
        "dataset": dataset_name,
        "total_queries": total,
        "success_rate_s": round(successful / total * 100, 2),
        "correct_path_rate_cp": round(correct_path / total * 100, 2),
        "avg_delta_sl": round(total_delta_sl / total, 2),
        "avg_step_recall": round(total_step_recall / total, 4),
        "avg_step_precision": round(total_step_precision / total, 4),
        "avg_step_f1": round(total_step_f1 / total, 4),
        "avg_planning_latency_s": round(sum(planning_latencies) / total, 3),
        "avg_total_latency_s": round(sum(total_latencies) / total, 3),
        "p50_planning_latency_s": round(p50_planning, 3),
        "p95_planning_latency_s": round(p95_planning, 3),
        "successful_queries": successful,
        "correct_path_queries": correct_path,
        "http_status_distribution": {str(k): v for k, v in sorted(status_counts.items())},
        "http_error_count": http_errors,
    }


def sanitize_path_component(value):
    """Clean a string to ensure it is safe to use in filesystem paths."""
    sanitized = re.sub(r'[<>:"/\\|?*\n\r\t]+', '_', str(value))
    sanitized = sanitized.strip(' .')
    return sanitized or 'unknown'


def write_responses(results, output_dir):
    """Persist the processed global responses to a final JSON file."""
    path = output_dir / "responses.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"  Responses: {path}")


def write_metrics(metrics, output_dir):
    """Persist the computed aggregate metrics to the final JSON file."""
    path = output_dir / "metrics.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2, ensure_ascii=False)
    print(f"  Metrics:   {path}")


def write_per_query_csv(results, output_dir):
    """Convert and save the per-query data in standard CSV format."""
    path = output_dir / "per_query.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "query_index", "query", "oracle_count", "planned_count",
            "delta_sl", "is_correct_path", "step_recall", "step_precision",
            "step_f1", "tasks_successful",
            "planning_latency_s", "total_latency_s", "http_status", "error",
        ])
        for r in results:
            writer.writerow([
                r["query_index"], r["query"], r["oracle_count"],
                r["planned_count"], r["delta_sl"], r["is_correct_path"],
                r.get("step_recall", ""), r.get("step_precision", ""),
                r.get("step_f1", ""), r["tasks_successful"], r["planning_latency_s"],
                r["total_latency_s"], r.get("http_status", ""), r.get("error", ""),
            ])
    print(f"  Per-query: {path}")


def write_summary(metrics, output_dir, run_label):
    """Generate and write a textual summary log file for quick reading."""
    path = output_dir / "summary.txt"
    lines = [
        "=" * 64,
        f"  POLLEN Test Runner — Summary",
        f"  Run: {run_label}",
        f"  Dataset: {metrics['dataset']}",
        "=" * 64,
        "",
        f"  Total queries:       {metrics['total_queries']}",
        f"  Successful (S):      {metrics['successful_queries']}",
        f"  Correct path (CP):   {metrics['correct_path_queries']}",
        f"  HTTP errors (4xx/5xx): {metrics.get('http_error_count', 0)}",
        "",
        f"  Success Rate (S%):         {metrics['success_rate_s']:.2f}%",
        f"  Correct Path Rate (CP%):   {metrics['correct_path_rate_cp']:.2f}%",
        f"  Avg Δ Solution Length:     {metrics['avg_delta_sl']:.2f}",
        "",
        "  Step-level (diagnostic, non-canonical):",
        f"    Avg Step Recall:    {metrics.get('avg_step_recall', 0.0):.3f}",
        f"    Avg Step Precision: {metrics.get('avg_step_precision', 0.0):.3f}",
        f"    Avg Step F1:        {metrics.get('avg_step_f1', 0.0):.3f}",
        f"  HTTP Status Distribution:  {metrics.get('http_status_distribution', {})}",
        "",
        "  Latency (planning):",
        f"    Average:  {metrics['avg_planning_latency_s']:.3f} s",
        f"    P50:      {metrics['p50_planning_latency_s']:.3f} s",
        f"    P95:      {metrics['p95_planning_latency_s']:.3f} s",
        "",
        f"  Latency (total):",
        f"    Average:  {metrics['avg_total_latency_s']:.3f} s",
        "",
    ]
    text = "\n".join(lines)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"  Summary:  {path}")


def main():
    """Main function. Initialise the benchmark test execution."""
    args = parse_args()

    datasets = discover_datasets(args.datasets)
    if not datasets:
        print("No dataset found. Check the 'datasets/' folder.")
        sys.exit(1)

    print(f"Datasets found: {list(datasets.keys())}")
    model = sanitize_path_component(args.model or os.environ.get("LLM_MODEL", "unknown"))
    run_id = sanitize_path_component(args.run_id or datetime.now().strftime("%Y%m%d_%H%M%S"))

    for dataset_name, queries in datasets.items():
        print(f"\n{'=' * 64}")
        print(f"  Dataset: {dataset_name} ({len(queries)} total queries)")
        print(f"{'=' * 64}")

        run_label = f"{dataset_name}_{model}_{run_id}"
        if args.output_dir:
            output_dir = Path(args.output_dir)
        else:
            output_dir = RESULTS_DIR / run_label

        # 1. Run the dataset against the CU endpoint
        results = evaluate_dataset(
            dataset_name, queries, args.control_url, args.max_queries, output_dir
        )

        # 2. Aggregate the total (RestGPT) metrics and enrich them with metadata
        metrics = compute_aggregate_metrics(results, dataset_name)
        metrics["model"] = model
        metrics["run_id"] = run_id
        metrics["control_url"] = args.control_url

        # 3. Finalise writing to disk
        write_responses(results, output_dir)
        write_metrics(metrics, output_dir)
        write_per_query_csv(results, output_dir)
        write_summary(metrics, output_dir, run_label)

        print(f"\n  Results successfully saved to: {output_dir}")

    print("\nBenchmark completed.")


if __name__ == "__main__":
    main()