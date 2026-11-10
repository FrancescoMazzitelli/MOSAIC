"""Shared utilities for the Designer evaluation harness.

The harness is intentionally dependency-light: only the Python standard library
is required.  The runner uses ``urllib`` for HTTP calls, so it can run in the same
environment as the rest of the evaluation scripts without extra packages.
"""

from __future__ import annotations

import csv
import json
import math
import re
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse


CATEGORIES = ("OUT_OF_DOMAIN", "AMBIGUOUS", "INVALID")
PREDICTION_COLUMNS = CATEGORIES + ("UNKNOWN", "NOT_REACHED", "ERROR")
HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")

_CATEGORY_ALIASES = {
    "out-of-domain": "OUT_OF_DOMAIN",
    "out_of_domain": "OUT_OF_DOMAIN",
    "out of domain": "OUT_OF_DOMAIN",
    "ood": "OUT_OF_DOMAIN",
    "ambiguous": "AMBIGUOUS",
    "under-specified": "AMBIGUOUS",
    "underspecified": "AMBIGUOUS",
    "invalid": "INVALID",
    "unknown": "UNKNOWN",
}

_ORACLE_RE = re.compile(
    r"\b(GET|POST|PUT|PATCH|DELETE)\s+([^\s,;]+)", re.IGNORECASE
)
_SERVICE_ID_RE = re.compile(r"^[a-z][a-z0-9-]*-mock$")


def normalize_category(value: str | None) -> str:
    """Return a canonical category label, or ``""`` when it is missing."""
    text = (value or "").strip()
    if not text:
        return ""
    upper = text.upper().replace("-", "_").replace(" ", "_")
    if upper in CATEGORIES or upper == "UNKNOWN":
        return upper
    return _CATEGORY_ALIASES.get(text.strip().lower(), text.strip().upper())


def read_csv(path: str | Path) -> list[dict[str, str]]:
    """Read a CSV and strip whitespace from headers and values."""
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        rows: list[dict[str, str]] = []
        for raw in reader:
            row = {}
            for key, value in raw.items():
                if key is None:
                    continue
                row[str(key).strip()] = (value or "").strip()
            rows.append(row)
    return rows


def write_csv(path: str | Path, rows: Iterable[dict[str, Any]],
              fieldnames: list[str]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def normalize_path(path: str) -> str:
    """Normalise an endpoint path for exact reuse checks.

    Examples
    --------
    ``http://host/rest/X/1.0/bin?status=ok`` -> ``/bin``
    ``/albums/{id}/tracks``                 -> ``/albums/{}/tracks``
    """
    text = (path or "").strip()
    if not text:
        return ""
    if "://" in text:
        parsed = urlparse(text)
        text = parsed.path
    text = text.split("?", 1)[0].split("#", 1)[0]
    text = re.sub(r"\{[^}/]+\}", "{}", text)
    text = re.sub(r"/+", "/", text)
    if len(text) > 1:
        text = text.rstrip("/")
    return text


def endpoint_key(method: str, path: str) -> str:
    return f"{(method or '').strip().upper()} {normalize_path(path)}"


def parse_endpoint_key(key: str) -> tuple[str, str] | None:
    match = re.match(r"^\s*([A-Za-z]+)\s+(.+?)\s*$", key or "")
    if not match:
        return None
    method = match.group(1).upper()
    if method not in HTTP_METHODS:
        return None
    return method, normalize_path(match.group(2))


def parse_oracle_ops(oracle: str) -> list[str]:
    """Extract normalised ``METHOD path`` operations from an oracle string."""
    keys: list[str] = []
    for method, path in _ORACLE_RE.findall(oracle or ""):
        key = endpoint_key(method, path)
        if key not in keys:
            keys.append(key)
    return keys


def discovered_op_keys(discovered: list[dict[str, Any]] | None) -> set[str]:
    keys: set[str] = set()
    for service in discovered or []:
        caps = service.get("capabilities") or []
        if isinstance(caps, dict):
            caps = list(caps.keys())
        for cap in caps:
            parsed = parse_endpoint_key(cap)
            if parsed:
                keys.add(endpoint_key(*parsed))
    return keys


def catalog_op_keys(catalog: dict[str, Any] | list[dict[str, Any]] | None) -> set[str]:
    services = catalog.get("services", []) if isinstance(catalog, dict) else catalog
    keys: set[str] = set()
    for service in services or []:
        caps = service.get("capabilities") or {}
        if isinstance(caps, list):
            caps = {cap: "" for cap in caps}
        for cap in caps:
            parsed = parse_endpoint_key(cap)
            if parsed:
                keys.add(endpoint_key(*parsed))
    return keys


def catalog_service_ids(catalog: dict[str, Any] | list[dict[str, Any]] | None) -> set[str]:
    services = catalog.get("services", []) if isinstance(catalog, dict) else catalog
    return {
        str(service.get("_id") or service.get("id") or "").strip()
        for service in services or []
        if service.get("_id") or service.get("id")
    }


def contract_endpoint_keys(contract: dict[str, Any] | None) -> list[str]:
    if not isinstance(contract, dict):
        return []
    keys: list[str] = []
    for endpoint in contract.get("suggested_endpoints") or []:
        if not isinstance(endpoint, dict):
            continue
        keys.append(endpoint_key(endpoint.get("method", ""), endpoint.get("path", "")))
    return keys


def contract_checks(contract: dict[str, Any] | None,
                    catalog_ids: set[str] | None = None,
                    catalog_ops: set[str] | None = None) -> dict[str, Any]:
    """Check the Designer hard constraints and simple reuse signals."""
    result: dict[str, Any] = {
        "contract_present": isinstance(contract, dict),
        "contract_hard_valid": False,
        "contract_issues": [],
        "contract_service_id": "",
        "contract_endpoint_count": 0,
        "reuse_exact_count": 0,
        "reuse_exact_any": False,
        "reuse_endpoint_rate": 0.0,
        "duplicate_service_id": False,
    }
    if not isinstance(contract, dict):
        result["contract_issues"].append("contract is null or not an object")
        return result

    issues: list[str] = []
    service_id = str(contract.get("service_id") or "").strip()
    result["contract_service_id"] = service_id
    if not _SERVICE_ID_RE.match(service_id):
        issues.append("service_id violates HC-3 (expected kebab-case with -mock)")

    if catalog_ids and service_id in catalog_ids:
        result["duplicate_service_id"] = True
        issues.append("service_id duplicates an existing catalog service (HC-1)")

    endpoints = contract.get("suggested_endpoints") or []
    if not isinstance(endpoints, list) or not endpoints:
        issues.append("suggested_endpoints is empty (HC-2)")
        endpoints = []
    result["contract_endpoint_count"] = len(endpoints)

    for index, endpoint in enumerate(endpoints):
        if not isinstance(endpoint, dict):
            issues.append(f"endpoint #{index} is not an object")
            continue
        method = str(endpoint.get("method") or "").upper()
        if method not in ("GET", "POST", "PUT", "DELETE"):
            issues.append(f"endpoint #{index} has invalid method '{method}' (HC-4)")
        if method == "GET" and not str(endpoint.get("response_schema") or "").strip():
            issues.append(f"GET endpoint #{index} has an empty response_schema (HC-5)")
        if not str(endpoint.get("path") or "").strip():
            issues.append(f"endpoint #{index} has an empty path")

    keys = contract_endpoint_keys(contract)
    if catalog_ops:
        reused = [key for key in keys if key in catalog_ops]
        result["reuse_exact_count"] = len(reused)
        result["reuse_exact_any"] = bool(reused)
        result["reuse_endpoint_rate"] = (
            len(reused) / len(keys) if keys else 0.0
        )

    result["contract_issues"] = issues
    result["contract_hard_valid"] = not issues
    return result


def ret_has_oracle(discovered: list[dict[str, Any]] | None, oracle: str) -> bool | None:
    """Whether all oracle operations are present in the retrieved services."""
    oracle_keys = parse_oracle_ops(oracle)
    if not oracle_keys:
        return None
    return set(oracle_keys).issubset(discovered_op_keys(discovered))


def wilson_ci(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total <= 0:
        return (0.0, 0.0)
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = (z / denominator) * math.sqrt(
        p * (1 - p) / total + z * z / (4 * total * total)
    )
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def macro_f1(gold: list[str], pred: list[str], labels: Iterable[str]) -> float:
    scores: list[float] = []
    for label in labels:
        tp = sum(1 for g, p in zip(gold, pred) if g == label and p == label)
        fp = sum(1 for g, p in zip(gold, pred) if g != label and p == label)
        fn = sum(1 for g, p in zip(gold, pred) if g == label and p != label)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        scores.append(f1)
    return sum(scores) / len(scores) if scores else 0.0


def weighted_kappa(a: list[int], b: list[int], min_score: int = 1,
                   max_score: int = 5) -> float | None:
    """Quadratic weighted Cohen's kappa for two ordinal raters."""
    if len(a) != len(b) or not a:
        return None
    size = max_score - min_score + 1
    observed = [[0.0] * size for _ in range(size)]
    for x, y in zip(a, b):
        observed[x - min_score][y - min_score] += 1
    total = float(len(a))
    row = [sum(observed[i]) for i in range(size)]
    col = [sum(observed[i][j] for i in range(size)) for j in range(size)]
    expected = [[row[i] * col[j] / total for j in range(size)] for i in range(size)]
    numerator = 0.0
    denominator = 0.0
    for i in range(size):
        for j in range(size):
            weight = ((i - j) ** 2) / ((size - 1) ** 2) if size > 1 else 0.0
            numerator += weight * observed[i][j]
            denominator += weight * expected[i][j]
    if denominator == 0:
        return 1.0
    return 1.0 - numerator / denominator


def icc21(pairs: list[tuple[float, float]]) -> float | None:
    """ICC(2,1) for two raters, absolute agreement."""
    if len(pairs) < 2:
        return None
    n = len(pairs)
    k = 2
    grand = sum(x + y for x, y in pairs) / (n * k)
    row_means = [(x + y) / k for x, y in pairs]
    col_means = [
        sum(x for x, _ in pairs) / n,
        sum(y for _, y in pairs) / n,
    ]
    ss_rows = k * sum((m - grand) ** 2 for m in row_means)
    ss_cols = n * sum((m - grand) ** 2 for m in col_means)
    ss_total = sum(
        (value - grand) ** 2
        for x, y in pairs
        for value in (x, y)
    )
    ss_error = ss_total - ss_rows - ss_cols
    ms_rows = ss_rows / (n - 1) if n > 1 else 0.0
    ms_cols = ss_cols / (k - 1) if k > 1 else 0.0
    ms_error = ss_error / ((n - 1) * (k - 1)) if n > 1 and k > 1 else 0.0
    denominator = ms_rows + (k - 1) * ms_error + k * (ms_cols - ms_error) / n
    if denominator == 0:
        return 1.0
    return (ms_rows - ms_error) / denominator


def load_json(path: str | Path) -> Any:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def dump_json(path: str | Path, payload: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)


def latest_run_dir(results_root: str | Path) -> Path:
    root = Path(results_root)
    candidates = sorted(
        [path for path in root.glob("run_*") if path.is_dir()],
        key=lambda path: path.stat().st_mtime,
    )
    if not candidates:
        raise FileNotFoundError(f"No run_* directories found under {root}")
    return candidates[-1]
