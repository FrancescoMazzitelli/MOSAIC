"""Loader for SOCBench-D.

The benchmark layout is::

    socbench-d/benchmark/socbenchd_<i>/<domain>/queries.json
    socbench-d/benchmark/socbenchd_<i>/<domain>/<service>/openapi.json

``queries.json`` is domain-level: each query is answered against the five
services of its domain, and ``endpoints`` is the gold set (method + path).

MOSAIC indexes two texts per service:
  * stage-1 text  -> the service description (``select_stage1_text`` in
    ``db-gateway/db-gateway.py``; there is no generated description here)
  * stage-2 text  -> the enriched capability text built by
    ``_build_enriched_text``: capability + parameter names + response fields
    (+ request fields for non-GET operations).

Everything is deterministic and read-only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

BENCHMARK_ROOT = Path(__file__).resolve().parents[2] / "socbench-d" / "benchmark"

HTTP_METHODS = ("get", "post", "put", "delete", "patch", "head", "options")


@dataclass(frozen=True)
class Endpoint:
    op_id: str            # "GET /energy-patterns"
    service_id: str
    capability: str       # description / summary / operationId
    enriched_text: str    # what the cross-encoder sees


@dataclass(frozen=True)
class Service:
    service_id: str
    title: str
    description: str
    text: str             # stage-1 text
    endpoints: tuple[Endpoint, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Query:
    index: int
    text: str
    gold: tuple[str, ...]


@dataclass(frozen=True)
class DomainCase:
    instance: str
    domain: str
    services: tuple[Service, ...]
    queries: tuple[Query, ...]


# ---------------------------------------------------------------------------
# OpenAPI helpers
# ---------------------------------------------------------------------------

def _collect_property_names(schema: object, out: list[str]) -> None:
    """Collect *property* names recursively; structural keys are ignored."""
    if not isinstance(schema, dict):
        return
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name, subschema in properties.items():
            if name not in out:
                out.append(name)
            _collect_property_names(subschema, out)
    items = schema.get("items")
    if isinstance(items, dict):
        _collect_property_names(items, out)
    for combiner in ("allOf", "anyOf", "oneOf"):
        for subschema in schema.get(combiner) or []:
            _collect_property_names(subschema, out)


def _parameter_text(parameters: Iterable[dict]) -> str:
    parts: list[str] = []
    for parameter in parameters or []:
        if not isinstance(parameter, dict):
            continue
        name = parameter.get("name")
        if not name:
            continue
        schema = parameter.get("schema") or {}
        enum = schema.get("enum") if isinstance(schema, dict) else None
        if enum:
            parts.append(f"{name} ({', '.join(str(v) for v in enum)})")
        else:
            parts.append(str(name))
    # stable de-duplication
    return ", ".join(dict.fromkeys(parts))


def _response_field_text(operation: dict) -> str:
    fields: list[str] = []
    for response in (operation.get("responses") or {}).values():
        if not isinstance(response, dict):
            continue
        for body in (response.get("content") or {}).values():
            if isinstance(body, dict):
                _collect_property_names(body.get("schema") or {}, fields)
    return ", ".join(dict.fromkeys(fields))


def _request_field_text(operation: dict) -> str:
    fields: list[str] = []
    request_body = operation.get("requestBody") or {}
    for body in (request_body.get("content") or {}).values():
        if isinstance(body, dict):
            _collect_property_names(body.get("schema") or {}, fields)
    return ", ".join(dict.fromkeys(fields))


def _enriched_text(operation: dict, capability: str, method: str) -> str:
    parts = [capability] if capability else []
    parameters = _parameter_text(operation.get("parameters") or [])
    if parameters:
        parts.append(f"| parameters: {parameters}")
    responses = _response_field_text(operation)
    if responses:
        parts.append(f"| response fields: {responses}")
    if method != "get":
        requests = _request_field_text(operation)
        if requests:
            parts.append(f"| request fields: {requests}")
    return " ".join(parts)


def _load_service(service_dir: Path) -> Service:
    spec = json.loads((service_dir / "openapi.json").read_text())
    info = spec.get("info") or {}
    title = (info.get("title") or "").strip()
    description = (info.get("description") or "").strip()

    # Equivalent of select_stage1_text() when no generated description exists.
    stage1_text = description or title

    endpoints: list[Endpoint] = []
    for path, path_item in (spec.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        # Path-level parameters are valid OpenAPI but are not operations.
        path_parameters = path_item.get("parameters") or []
        for method, operation in path_item.items():
            if method not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            capability = (
                operation.get("description")
                or operation.get("summary")
                or operation.get("operationId")
                or ""
            ).strip()
            # Merge path-level parameters into the operation, as a client would.
            merged = dict(operation)
            merged["parameters"] = list(path_parameters) + list(
                operation.get("parameters") or []
            )
            endpoints.append(
                Endpoint(
                    op_id=f"{method.upper()} {path}",
                    service_id=service_dir.name,
                    capability=capability,
                    enriched_text=_enriched_text(merged, capability, method),
                )
            )

    return Service(
        service_id=service_dir.name,
        title=title,
        description=description,
        text=stage1_text,
        endpoints=tuple(endpoints),
    )


def _load_domain(instance_dir: Path, domain_dir: Path) -> DomainCase:
    service_dirs = sorted(
        p for p in domain_dir.iterdir() if p.is_dir() and (p / "openapi.json").exists()
    )
    services = tuple(_load_service(p) for p in service_dirs)

    raw_queries = json.loads((domain_dir / "queries.json").read_text())["queries"]
    queries = tuple(
        Query(
            index=i,
            text=item["query"],
            gold=tuple(item["endpoints"]),
        )
        for i, item in enumerate(raw_queries)
    )
    return DomainCase(
        instance=instance_dir.name,
        domain=domain_dir.name,
        services=services,
        queries=queries,
    )


def load_cases(
    instances: Iterable[str] | None = None,
    max_cases: int | None = None,
) -> list[DomainCase]:
    """Load the benchmark in deterministic order.

    ``max_cases`` is meant for smoke tests (e.g. one domain).
    """
    wanted = set(instances) if instances else None
    cases: list[DomainCase] = []
    for instance_dir in sorted(BENCHMARK_ROOT.glob("socbenchd_*")):
        if wanted and instance_dir.name not in wanted:
            continue
        for domain_dir in sorted(p for p in instance_dir.iterdir() if p.is_dir()):
            cases.append(_load_domain(instance_dir, domain_dir))
            if max_cases is not None and len(cases) >= max_cases:
                return cases
    return cases


if __name__ == "__main__":
    cases = load_cases()
    n_services = sum(len(c.services) for c in cases)
    n_endpoints = sum(len(s.endpoints) for c in cases for s in c.services)
    n_queries = sum(len(c.queries) for c in cases)
    n_gold = sum(len(q.gold) for c in cases for q in c.queries)
    print(
        f"cases={len(cases)} services={n_services} endpoints={n_endpoints} "
        f"queries={n_queries} gold={n_gold}"
    )
