"""Shared helpers for the Docker-stack (db-gateway) evaluation.

The gateway catalog is global, so every SOCBench-D service needs a unique id.
The benchmark itself reuses service directory names across instances/domains
(e.g. ``01-energy`` exists in every instance), therefore the id is built from
instance + domain + directory and sanitised to ``[A-Za-z0-9_.-]``.

The mapping is deterministic, so the registration script and the evaluation
script compute exactly the same ids without sharing state.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote_plus

from data import BENCHMARK_ROOT, DomainCase, Service, load_cases

GATEWAY_URL = "http://localhost:5000"
IMPORTER_URL = "http://localhost:7500"
MOCK_SERVER_URL = "http://mock-server:8080"

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def service_uid(case: DomainCase, service: Service) -> str:
    raw = f"{case.instance}__{case.domain}__{service.service_id}"
    uid = _SAFE.sub("_", raw).strip("_")
    if not uid:
        raise ValueError(f"empty service uid for {raw!r}")
    return uid


def service_spec_path(case: DomainCase, service: Service) -> Path:
    return (
        BENCHMARK_ROOT
        / case.instance
        / case.domain
        / service.service_id
        / "openapi.json"
    )


def mock_base_url(title: str) -> str:
    """Microcks mock base URL for a service title (spaces -> '+', '&' -> %26)."""
    return f"{MOCK_SERVER_URL}/rest/{quote_plus(title)}/1.0"


def iter_services(cases: list[DomainCase]):
    """Yield ``(case, service, uid, spec_path)`` in deterministic order."""
    for case in cases:
        for service in case.services:
            yield case, service, service_uid(case, service), service_spec_path(case, service)


def cases_for(instances: list[str] | None = None, max_cases: int | None = None):
    return load_cases(instances=instances, max_cases=max_cases)
