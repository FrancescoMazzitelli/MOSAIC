#!/usr/bin/env python3
"""Run the Designer diagnostic evaluation.

The runner sends each query to the control unit, stores the raw response, and
writes one row per query.  It supports ``dry_run`` so write-side plans are
validated but not executed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from Eval.designer_eval.common import (
    CATEGORIES,
    contract_checks,
    dump_json,
    normalize_category,
    read_csv,
    ret_has_oracle,
    write_csv,
)


HERE = Path(__file__).resolve().parent
DEFAULT_CONTROL_URL = "http://localhost:5500/api/control/invoke"
PROPOSED_QUERIES = HERE / "queries" / "designer_queries.csv"
ADJUDICATED_QUERIES = HERE / "queries" / "designer_queries_adjudicated.csv"
DEFAULT_QUERIES = ADJUDICATED_QUERIES if ADJUDICATED_QUERIES.exists() else PROPOSED_QUERIES
DEFAULT_SMART_CITY = HERE / "queries" / "smart_city_100.csv"
DEFAULT_RESULTS = HERE / "results"
DEFAULT_CATALOG = HERE / "catalog_snapshot.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Designer evaluation runner (45 diagnostic + 100 in-domain)"
    )
    parser.add_argument("--control-url", default=DEFAULT_CONTROL_URL)
    parser.add_argument("--queries", default=str(DEFAULT_QUERIES))
    parser.add_argument("--smart-city", default=str(DEFAULT_SMART_CITY))
    parser.add_argument("--catalog", default=str(DEFAULT_CATALOG),
                        help="Optional catalog snapshot for HC-1 and reuse checks")
    parser.add_argument("--out-dir", default=None,
                        help="Run directory; default results/run_<timestamp>")
    parser.add_argument("--timeout", type=int, default=150)
    parser.add_argument("--delay", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--limit", type=int, default=None,
                        help="Debug: run only the first N selected queries")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                        default=True, help="Do not execute plans (default)")
    parser.add_argument("--no-dry-run", dest="dry_run", action="store_false",
                        help="Execute plans (may mutate mock data)")
    parser.add_argument("--allow-proposed", action="store_true",
                        help="Allow running before labels are adjudicated")
    return parser.parse_args()


def post_json(url: str, payload: dict[str, Any],
              timeout: int) -> tuple[dict[str, Any] | None, str | None, int | None, float]:
    started = time.perf_counter()
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            status = response.status
        return json.loads(body), None, status, time.perf_counter() - started
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return None, f"HTTP {exc.code}: {body[:500]}", exc.code, time.perf_counter() - started
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        return None, f"Connection/timeout error: {exc}", None, time.perf_counter() - started
    except json.JSONDecodeError as exc:
        return None, f"Invalid JSON response: {exc}", None, time.perf_counter() - started


def load_records(args: argparse.Namespace) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []

    diagnostic = read_csv(args.queries)
    for row in diagnostic:
        row = dict(row)
        row["set"] = "diagnostic"
        row["gold_category"] = normalize_category(row.get("gold_category"))
        row["label_status"] = (row.get("label_status") or "proposed").strip().lower()
        if not row.get("query_id"):
            raise ValueError(f"Missing query_id in {args.queries}")
        if row["gold_category"] not in CATEGORIES:
            raise ValueError(
                f"Invalid gold_category for {row['query_id']}: "
                f"{row.get('gold_category')!r}"
            )
        records.append(row)

    smart = read_csv(args.smart_city)
    for row in smart:
        query = row.get("query") or row.get("question") or row.get("Questions") or ""
        records.append({
            "set": "smart_city",
            "query_id": row.get("query_id") or f"SC{len(records) + 1:03d}",
            "query": query,
            "gold_category": "IN_DOMAIN",
            "subtype": row.get("category") or "",
            "expected_behavior": "plan",
            "source": row.get("source") or "smart_city_100.csv",
            "contamination_flag": "no",
            "label_status": "in_domain",
            "oracle": row.get("oracle") or row.get("Oracle") or "",
        })

    random.Random(args.seed).shuffle(records)
    if args.limit:
        records = records[: args.limit]
    return records


def infer_route_reason(data: dict[str, Any], reached: bool,
                       planner_tasks: int | None,
                       tasks_after: int | None,
                       error: str | None) -> str:
    explicit = (data.get("route_reason") or "").strip()
    if explicit:
        return explicit
    if error and "No services matched" in error:
        return "NO_SERVICES"
    if error and "None of the discovered services" in error:
        return "REGISTRY_EMPTY"
    if reached:
        return "PLANNER_EMPTY_DESIGNER"
    if planner_tasks is None and tasks_after is None:
        return "PLANNER_MALFORMED"
    if planner_tasks is not None and planner_tasks > 0:
        if tasks_after == 0:
            return "POST_VALIDATION_EMPTY"
        return "PLANNER_NONEMPTY"
    if planner_tasks == 0 and tasks_after == 0:
        return "POST_VALIDATION_EMPTY"
    return "UNKNOWN"


def extract_response_row(record: dict[str, str],
                         data: dict[str, Any] | None,
                         error: str | None,
                         http_status: int | None,
                         latency: float,
                         catalog_ids: set[str],
                         catalog_ops: set[str],
                         instrumented_server: bool | None) -> dict[str, Any]:
    record = dict(record)
    data = data or {}
    plan = data.get("execution_plan")
    tasks_after = (
        plan.get("tasks")
        if isinstance(plan, dict) and isinstance(plan.get("tasks"), list)
        else None
    )
    tasks_after_count = len(tasks_after) if tasks_after is not None else None

    raw_planner_count = data.get("planner_tasks_count")
    try:
        planner_tasks_count = int(raw_planner_count) if raw_planner_count is not None else None
    except (TypeError, ValueError):
        planner_tasks_count = None
    if planner_tasks_count is None and tasks_after_count is not None:
        # Compatibility with the non-instrumented control unit.
        planner_tasks_count = tasks_after_count

    raw_validated_count = data.get("validated_tasks_count")
    try:
        validated_tasks_count = int(raw_validated_count) if raw_validated_count is not None else None
    except (TypeError, ValueError):
        validated_tasks_count = None
    if validated_tasks_count is None:
        validated_tasks_count = tasks_after_count

    predicted = normalize_category(data.get("empty_plan_category"))
    reached = bool(predicted)
    contracts = data.get("suggested_api_contracts") or []
    contract = contracts[0] if isinstance(contracts, list) and contracts and isinstance(contracts[0], dict) else None
    checks = contract_checks(contract, catalog_ids, catalog_ops)
    discovered = data.get("discovered") or []
    route_reason = infer_route_reason(
        data, reached, planner_tasks_count, validated_tasks_count, error
    )

    row: dict[str, Any] = {
        **record,
        "http_status": http_status if http_status is not None else "",
        "latency_s": round(latency, 3),
        "network_error": error or "",
        "instrumented_server": "" if instrumented_server is None else int(instrumented_server),
        "reached_designer": int(reached),
        "predicted_category": predicted,
        "route_reason": route_reason,
        "planner_status": data.get("planner_status") or "",
        "planner_tasks_count": "" if planner_tasks_count is None else planner_tasks_count,
        "validated_tasks_count": "" if validated_tasks_count is None else validated_tasks_count,
        "tasks_after_count": "" if tasks_after_count is None else tasks_after_count,
        "contract_present": int(checks["contract_present"]),
        "contract_hard_valid": int(checks["contract_hard_valid"]),
        "contract_issues": " | ".join(checks["contract_issues"]),
        "contract_service_id": checks["contract_service_id"],
        "contract_endpoint_count": checks["contract_endpoint_count"],
        "contract_json": json.dumps(contract, ensure_ascii=False) if contract else "",
        "discovered_json": json.dumps(discovered, ensure_ascii=False),
        "ret_has_gold": (
            "" if (ret_gold := ret_has_oracle(discovered, record.get("oracle", ""))) is None
            else int(bool(ret_gold))
        ),
        "error": data.get("error") or "",
        "empty_plan_justification": data.get("empty_plan_justification") or "",
    }
    return row


def git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        return ""


def sha256_file(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def prompt_hashes() -> dict[str, str | None]:
    repo_root = HERE.parents[1]
    paths = [
        repo_root / "control-unit" / "service" / "designerService.py",
        repo_root / "control-unit" / "service" / "planning" / "prompt_provider.py",
        repo_root / "control-unit" / "config" / "prompts" / "planner_system.md",
        repo_root / "control-unit" / "config" / "prompts" / "planner_ablation.md",
        repo_root / "control-unit" / "config" / "prompts" / "planner_runner.md",
    ]
    return {str(path.relative_to(repo_root)): sha256_file(path) for path in paths}


def write_per_query(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "run_id", "set", "query_id", "query", "gold_category", "subtype",
        "expected_behavior", "source", "contamination_flag", "label_status",
        "oracle", "http_status", "latency_s", "network_error",
        "instrumented_server", "reached_designer", "predicted_category",
        "route_reason", "planner_status", "planner_tasks_count",
        "validated_tasks_count", "tasks_after_count", "contract_present",
        "contract_hard_valid", "contract_issues", "contract_service_id",
        "contract_endpoint_count", "contract_json", "discovered_json",
        "ret_has_gold", "error", "empty_plan_justification",
    ]
    write_csv(path, rows, fields)


def main() -> int:
    args = parse_args()
    run_id = datetime.now().strftime("run_%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir) if args.out_dir else DEFAULT_RESULTS / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    catalog_ids: set[str] = set()
    catalog_ops: set[str] = set()
    catalog_payload: Any = None
    catalog_path = Path(args.catalog)
    if catalog_path.exists():
        try:
            catalog_payload = json.loads(catalog_path.read_text(encoding="utf-8"))
            from Eval.designer_eval.common import catalog_op_keys, catalog_service_ids
            catalog_ids = catalog_service_ids(catalog_payload)
            catalog_ops = catalog_op_keys(catalog_payload)
        except Exception as exc:
            print(f"[WARN] Cannot read catalog ({catalog_path}): {exc}", file=sys.stderr)
    else:
        print(
            f"[WARN] No catalog snapshot at {catalog_path}; "
            "HC-1 and reuse will be computed only on the discovered services.",
            file=sys.stderr,
        )

    records = load_records(args)
    diag_counts = {
        category: sum(1 for row in records if row.get("gold_category") == category)
        for category in CATEGORIES
    }
    if not args.limit and any(count != 15 for count in diag_counts.values()):
        print(
            f"[WARN] Unexpected balance in the diagnostic set: {diag_counts}. "
            "The expected setup is 15/15/15.",
            file=sys.stderr,
        )
    if any(
        row.get("set") == "diagnostic" and row.get("label_status") != "adjudicated"
        for row in records
    ) and not args.allow_proposed:
        print(
            "[WARN] The diagnostic queries are not yet adjudicated. "
            "Running anyway; use --allow-proposed to silence this warning.",
            file=sys.stderr,
        )

    manifest = {
        "run_id": run_id,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "control_url": args.control_url,
        "queries": str(Path(args.queries).resolve()),
        "smart_city": str(Path(args.smart_city).resolve()),
        "catalog": str(catalog_path.resolve()) if catalog_path.exists() else None,
        "catalog_sha256": sha256_file(catalog_path) if catalog_path.exists() else None,
        "dry_run": args.dry_run,
        "timeout_s": args.timeout,
        "seed": args.seed,
        "git_commit": git_commit(),
        "prompt_hashes": prompt_hashes(),
        "env": {
            key: os.environ.get(key)
            for key in (
                "LLM_MODEL", "DESIGNER_LLM_MODEL", "DESIGNER_TEMPERATURE",
                "DESIGNER_SEED", "DESIGNER_NUM_CTX", "USE_LLM_DECOMPOSITION",
                "TOP_ENDPOINTS_PER_SERVICE", "BACKEND_MODE", "RERANK_MODEL",
            )
        },
        "diag_counts": diag_counts,
        "total_queries": len(records),
    }
    dump_json(out_dir / "run_manifest.json", manifest)

    raw_path = out_dir / "raw_responses.jsonl"
    rows: list[dict[str, Any]] = []
    instrumented_server: bool | None = None

    with open(raw_path, "a", encoding="utf-8") as raw_handle:
        for index, record in enumerate(records, 1):
            print(
                f"[{index:03d}/{len(records):03d}] {record['set']:10s} "
                f"{record['query_id']:>5s}  {record['query'][:90]}"
            )
            payload = {"input": record["query"], "dry_run": args.dry_run}
            data, error, status, latency = post_json(
                args.control_url, payload, args.timeout
            )
            if data is not None:
                instrumented_server = bool(
                    "route_reason" in data or "planner_tasks_count" in data
                )
            row = extract_response_row(
                record, data, error, status, latency, catalog_ids, catalog_ops,
                instrumented_server,
            )
            row["run_id"] = run_id
            rows.append(row)
            write_per_query(out_dir / "per_query.csv", rows)
            raw_handle.write(json.dumps({
                "run_id": run_id,
                "set": record["set"],
                "query_id": record["query_id"],
                "query": record["query"],
                "http_status": status,
                "latency_s": round(latency, 3),
                "error": error,
                "response": data,
            }, ensure_ascii=False) + "\n")
            raw_handle.flush()
            if args.delay:
                time.sleep(args.delay)

    write_per_query(out_dir / "per_query.csv", rows)
    manifest["finished_at"] = datetime.now().isoformat(timespec="seconds")
    manifest["instrumented_server"] = instrumented_server
    dump_json(out_dir / "run_manifest.json", manifest)

    print(f"\nResults written to: {out_dir}")
    print(f"  raw:       {raw_path}")
    print(f"  per-query: {out_dir / 'per_query.csv'}")
    print("\nAnalysis:")
    print(
        f"  python {Path(__file__).name} --help\n"
        f"  python {HERE / 'analyze_designer_eval.py'} --run-dir {out_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
