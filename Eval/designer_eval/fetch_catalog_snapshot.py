#!/usr/bin/env python3
"""Fetch a snapshot of the full service catalog for reuse/HC-1 checks."""

from __future__ import annotations

import argparse
import json
import socket
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from Eval.designer_eval.common import dump_json


HERE = Path(__file__).resolve().parent
DEFAULT_OUT = HERE / "catalog_snapshot.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch /services catalog snapshot")
    parser.add_argument("--catalog-url", default="http://localhost:5000")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--timeout", type=int, default=30)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    url = args.catalog_url.rstrip("/") + "/services"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            body = response.read().decode("utf-8")
            status = response.status
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"HTTP {exc.code} from {url}: {exc.read()[:500]!r}")
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        raise SystemExit(f"Cannot reach {url}: {exc}")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON from {url}: {exc}")

    services: Any
    if isinstance(payload, list):
        services = payload
    elif isinstance(payload, dict) and isinstance(payload.get("services"), list):
        services = payload["services"]
    else:
        raise SystemExit(
            f"Unexpected payload shape from {url}: {type(payload).__name__}"
        )

    snapshot = {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "source": url,
        "http_status": status,
        "services": services,
    }
    out = Path(args.out)
    dump_json(out, snapshot)
    print(f"Saved {len(services)} services to {out}")
    print(f"Source: {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
