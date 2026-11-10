"""Register every SOCBench-D service into the running MOSAIC stack.

The flow is the same used by the smart-city demo:

  OpenAPI file -> api-importer /import/file
      -> parse_swagger()  (capabilities, endpoints, schemas, parameters)
      -> register_to_redis()
      -> register_to_consul()
      -> catalog-gateway POST /service  (MongoDB + Qdrant, Stage 1 and Stage 2)

Ids are unique per (instance, domain, service directory); see gateway_common.py.

Usage::

    python register_gateway.py                 # all 275 services
    python register_gateway.py --max-cases 1   # 5 services (smoke test)
    python register_gateway.py --force         # re-index existing services
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from tqdm import tqdm

from gateway_common import (
    GATEWAY_URL,
    IMPORTER_URL,
    cases_for,
    iter_services,
    mock_base_url,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instances", nargs="*", default=None)
    parser.add_argument("--max-cases", type=int, default=None)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--out-dir", type=Path, default=Path("results"))
    return parser.parse_args()


def already_registered(uid: str) -> bool:
    """True if the service is already in the catalog.

    The gateway can be restarted while a long registration is running, so
    retry a few times instead of treating a transient connection error as
    "not registered" (which would re-import the service unnecessarily).
    """
    for attempt in range(3):
        try:
            response = requests.get(f"{GATEWAY_URL}/services/{uid}", timeout=30)
            return response.status_code == 200
        except requests.RequestException:
            time.sleep(2 * (attempt + 1))
    return False


def register_one(item: tuple) -> dict:
    case, service, uid, path = item
    if not path.exists():
        return {"uid": uid, "status": "missing_spec", "path": str(path)}

    if not already_registered(uid):
        body = None
        last_error: Exception | None = None
        # The importer/gateway may be briefly unavailable (container restart,
        # OOM, environment reset). Retry the upload instead of aborting the
        # whole run: the benchmark registration is idempotent.
        for attempt in range(5):
            try:
                with path.open("rb") as handle:
                    files = {"file": (path.name, handle, "application/json")}
                    data = {"id": uid, "base_url": mock_base_url(service.title)}
                    response = requests.post(
                        f"{IMPORTER_URL}/api/importer/import/file",
                        files=files,
                        data=data,
                        timeout=300,
                    )
                if response.status_code != 200:
                    return {
                        "uid": uid,
                        "status": f"http_{response.status_code}",
                        "error": response.text[:500],
                        "path": str(path),
                    }
                body = response.json()
                break
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                time.sleep(2 * (attempt + 1))
        if body is None:
            return {
                "uid": uid,
                "status": "connection_error",
                "error": repr(last_error),
                "path": str(path),
            }
    else:
        body = {"status": "already_present", "endpoints": None}

    return {
        "uid": uid,
        "status": body.get("status", "ok"),
        "instance": case.instance,
        "domain": case.domain,
        "service": service.service_id,
        "title": service.title,
        "n_expected": len(service.endpoints),
        "n_registered": body.get("endpoints"),
        "path": str(path),
    }


def main() -> None:
    args = parse_args()
    cases = cases_for(instances=args.instances, max_cases=args.max_cases)
    items = list(iter_services(cases))
    if not items:
        raise SystemExit("no service found")

    # Fail fast if the stack is not reachable.
    for name, url in (("gateway", f"{GATEWAY_URL}/health"), ("importer", f"{IMPORTER_URL}/swagger")):
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(f"{name} not reachable at {url}: {exc}") from exc

    print(f"services={len(items)} cases={len(cases)} workers={args.workers} force={args.force}")
    if args.dry_run:
        for case, service, uid, path in items[:10]:
            print(f"  {uid}  ({len(service.endpoints)} endpoints)  {path}")
        print(f"  ... {max(0, len(items) - 10)} more")
        return

    rows: list[dict] = []
    started = time.time()
    if args.workers <= 1:
        iterator = (register_one(item) for item in tqdm(items, desc="services"))
        rows = list(iterator)
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(register_one, item): item for item in items}
            for future in tqdm(as_completed(futures), total=len(futures), desc="services"):
                item = futures[future]
                try:
                    rows.append(future.result())
                except Exception as exc:  # noqa: BLE001 - keep the run alive
                    rows.append(
                        {
                            "uid": item[2],
                            "status": "worker_error",
                            "error": repr(exc),
                            "path": str(item[3]),
                        }
                    )

    failures = [row for row in rows if row["status"] not in ("ok", "already_present")]
    mismatches = [
        row
        for row in rows
        if row.get("n_registered") is not None
        and row.get("n_expected") is not None
        and row["n_registered"] != row["n_expected"]
    ]

    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.out_dir / "gateway_registered.json"
    out_path.write_text(json.dumps(rows, indent=2, ensure_ascii=False))

    print(f"\nelapsed: {(time.time() - started) / 60:.1f} min")
    print(f"ok={len(rows) - len(failures)} failures={len(failures)} endpoint_mismatches={len(mismatches)}")
    if failures:
        print("\nFAILURES:")
        for row in failures[:20]:
            print(f"  {row['uid']}: {row['status']} {row.get('error', '')[:200]}")
    if mismatches:
        print("\nENDPOINT COUNT MISMATCHES (expected vs registered):")
        for row in mismatches[:20]:
            print(f"  {row['uid']}: {row['n_expected']} vs {row['n_registered']}")
    print(f"\nwrote {out_path}")
    if failures or mismatches:
        sys.exit(1)


if __name__ == "__main__":
    main()
