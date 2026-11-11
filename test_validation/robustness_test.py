#!/usr/bin/env python3
"""
robustness_test.py
==================

ROBUSTNESS test of discovery under increasing noise, in a SINGLE runner.

At each distractor step (50, 100, 150, 200) it measures TWO levels:

  • RETRIEVAL (gateway)   - POST /index/search. Measures the coverage of the
      correct endpoints and the intrusion of distractors into the ranking.
      It also captures the cases where the correct service is retrieved but the
      right endpoint may have been pruned by the reranker / token budget.
  • END-TO-END (control)  - POST /api/control/invoke: does the LLM select
        the right endpoint in the plan? (selection_accuracy, micro-F1, final_correct).
        Reuses Layer-1 from test_validation/testRunner.py.

TWO SCOPING MODES (SCOPE_MODE):
  • import  (default) - distractors are imported in steps (offset).
                        Requires a CLEAN initial catalog.
  • filter            - the 200 distractors are ALREADY in the catalog and tagged with
                        sample_rank (see backfill_ranks); the level becomes the
                        max_rank parameter passed to gateway/control-unit. No
                        import, no deletions, re-runnable indefinitely.

USAGE
-----
    python robustness_test.py
    (PowerShell:  $env:SCOPE_MODE="filter"; $env:PHASES="retrieval"; python robustness_test.py)

PREREQUISITES
-------------
# 1. import : CLEAN stack (mock smart-city only).

# 2. reindex: puts the distractors into services_index → the RETRIEVAL phase sees them
Invoke-RestMethod -Method Post http://localhost:5000/index/reindex

# 3. backfill: writes sample_rank to Mongo + Qdrant → enables the max_rank filter
Invoke-RestMethod -Method Post http://localhost:7500/api/importer/backfill_ranks

# 4. register_existing: puts the distractors back into Consul+Redis → the END-TO-END phase sees them
Invoke-RestMethod -Method Post http://localhost:7500/api/importer/register_existing

Environment variables (default in parentheses):
    SCOPE_MODE      (import)   import | filter --> filter if the distractors are already in the catalog
    PHASES          (retrieval,control)
    GATEWAY_URL     (http://localhost:5000)
    CONTROL_URL     (http://localhost:5500/api/control/invoke)
    IMPORTER_URL    (http://localhost:7500)
    REQUESTS_CSV    (test_validation/smart_city_test_requests.csv)
    MIN_DISTRACTORS (=STEP)   MAX_DISTRACTORS (200)   STEP (50)   SEED (42)
    MAX_QUERIES     (0 = all)   TIMEOUT (180)   GATEWAY_TIMEOUT (600)   DELAY (0.3)
    OUT_CSV         (auto: risultati_test/robustness_<phases>_<type>_<scope>_<date>.csv)
"""

import csv
import os
import re
import sys
import time
import json
import importlib.util
import requests

HERE            = os.path.dirname(os.path.abspath(__file__))
SCOPE_MODE      = os.environ.get("SCOPE_MODE", "import").strip().lower()   # import | filter
PHASES          = {p.strip().lower() for p in os.environ.get("PHASES", "retrieval,control").split(",") if p.strip()}
GATEWAY_URL     = os.environ.get("GATEWAY_URL",  "http://localhost:5000")
CONTROL_URL     = os.environ.get("CONTROL_URL",  "http://localhost:5500/api/control/invoke")
IMPORTER_URL    = os.environ.get("IMPORTER_URL", "http://localhost:7500")
REQUESTS_CSV    = os.environ.get("REQUESTS_CSV", os.path.join(HERE, "smart_city_test_requests.csv"))
MAX_DISTRACTORS = int(os.environ.get("MAX_DISTRACTORS", "200"))
STEP            = int(os.environ.get("STEP", "50"))
MIN_DISTRACTORS = int(os.environ.get("MIN_DISTRACTORS", str(STEP)))
SEED            = int(os.environ.get("SEED", "42"))
MAX_QUERIES     = int(os.environ.get("MAX_QUERIES", "0"))     # 0 = all
# Filter on the query type: "all" (default) | "cross" (cross-service only:
# oracle touching >1 service) | "single" (a single service).
QUERY_FILTER    = os.environ.get("QUERY_FILTER", "all").strip().lower()
# On cross-service queries the "primary" metrics svc/ep_recall (tied to the first
# service of the chain) are partial: in that case we omit them and keep only
# coverage + intrusion + end-to-end.
CROSS_ONLY      = (QUERY_FILTER == "cross")
TIMEOUT         = int(os.environ.get("TIMEOUT", "1200"))        # control-unit (LLM)
GATEWAY_TIMEOUT = int(os.environ.get("GATEWAY_TIMEOUT", "600"))
DELAY           = float(os.environ.get("DELAY", "0.3"))


def _default_out_csv():
    """Auto CSV name: robustness_<phases>_<type>_<scope>_<YYYYMMDD>.csv.
    So runs from different dates or types do not overwrite each other. Override with OUT_CSV."""
    date  = time.strftime("%Y%m%d")
    parts = ["robustness", "-".join(sorted(PHASES)) if PHASES else "none"]
    if QUERY_FILTER != "all":
        parts.append(QUERY_FILTER)
    parts.append(SCOPE_MODE)
    out_dir = os.path.join(HERE, "risultati_test")
    os.makedirs(out_dir, exist_ok=True)
    return os.path.join(out_dir, f"{'_'.join(parts)}_{date}.csv")


OUT_CSV         = os.environ.get("OUT_CSV") or _default_out_csv()




# ---------------------------------------------------------------------------
# Reuse of the canonical evaluator (testRunner.py) - only if the control phase is needed
# ---------------------------------------------------------------------------

def _load_testrunner():
    path = os.path.join(HERE, "testRunner.py")
    if not os.path.isfile(path):
        sys.exit(f"[ERROR] testRunner.py not found at {path} (required by the 'control' phase)")
    spec = importlib.util.spec_from_file_location("testRunner", path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

TR = _load_testrunner() if "control" in PHASES else None


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------

def _norm_op(raw):
    return " ".join((raw or "").replace("\\n", " ").split())


def _op_parts(op):
    """ 'GET /bin/1' -> ('GET', '/bin/1'). Valid for both oracle and capability. """
    parts = op.split(None, 1)
    if len(parts) != 2:
        return (op.upper(), "")
    return (parts[0].upper(), parts[1].strip().rstrip('/'))


def _resource_seg(op):
    _, path = _op_parts(op)
    segs = [s for s in path.lstrip("/").split("/") if s]
    return segs[0].lower() if segs else ""


def _path_seg(path):
    segs = [s for s in (path or "").lstrip("/").split("/") if s]
    return segs[0].lower() if segs else ""


def _is_cross_service(steps):
    """Cross-service = the oracle touches more than one service (distinct
    resource segment). E.g. [GET /vehicle, GET /sensor] → cross; [GET /bin, DELETE /bin/1]
    → single (same service)."""
    segs = {_path_seg(p) for _, p in steps if p}
    return len(segs) > 1


def _path_match(cap_path, ora_path):
    """Path match tolerant to placeholders on BOTH sides:
       '/bin/{id}' vs '/bin/1' -> True ; '/bin' vs '/bin/1' -> False."""
    cs  = [s for s in cap_path.strip('/').split('/') if s]
    osg = [s for s in ora_path.strip('/').split('/') if s]
    if len(cs) != len(osg):
        return False
    for a, b in zip(cs, osg):
        if a.startswith('{') or b.startswith('{'):
            continue
        if a != b:
            return False
    return True


def _caps_match_op(caps_keys, o_method, o_path):
    for cap_key in caps_keys:
        m, p = _op_parts(cap_key)
        if m == o_method and _path_match(p, o_path):
            return True
    return False


def _op_present_anywhere(services, caps_getter, o_method, o_path):
    """True if at least ONE discovered service exposes the operation (cross-service).
    Used for multi-step coverage, where the steps may live on different
    services and therefore cannot be looked up only in the expected service."""
    for svc in services:
        if _caps_match_op(caps_getter(svc), o_method, o_path):
            return True
    return False


def parse_oracle_steps(raw):
    if TR is not None:
        return TR.parse_oracle(raw)
    cleaned = (raw or "").replace('"', '').replace('\\n', '\n').replace('\\r', '')
    out = []
    for line in cleaned.strip().splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) == 2:
            out.append((parts[0].upper(), parts[1].strip().rstrip('/')))
        elif len(parts) == 1:
            out.append((parts[0].upper(), ""))
    return out


def load_queries(path):
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for r in reader:
            q_key = next((k for k in r if "question" in k.lower()), None)
            o_key = next((k for k in r if "oracle"   in k.lower()), None)
            if not q_key:
                continue
            q = (r.get(q_key) or "").strip()
            if not q:
                continue
            oracle_raw = (r.get(o_key) or "") if o_key else ""
            steps = parse_oracle_steps(oracle_raw)
            first = _norm_op(oracle_raw.replace("\\n", "\n").splitlines()[0]) if oracle_raw.strip() else ""
            rows.append({"query": q, "oracle_steps": steps, "seg": _resource_seg(first)})

    # Filter by query type (cross-service / single) BEFORE the MAX_QUERIES cap,
    # so "30 cross-service queries" = 30 actual queries of that type.
    if QUERY_FILTER == "cross":
        rows = [r for r in rows if _is_cross_service(r["oracle_steps"])]
    elif QUERY_FILTER == "single":
        rows = [r for r in rows if not _is_cross_service(r["oracle_steps"])]

    if MAX_QUERIES > 0:
        rows = rows[:MAX_QUERIES]
    return rows


def import_distractors(limit, offset=0):
    """Import ONLY the new slice [offset:limit]. Used only in SCOPE_MODE=import."""
    if limit <= offset:
        return
    print(f"[IMPORT] importing distractors [{offset}:{limit}] (seed={SEED})...")
    t0 = time.time()
    r = requests.post(f"{IMPORTER_URL}/api/importer/import",
                      json={"limit": limit, "seed": SEED, "offset": offset}, timeout=3600)
    r.raise_for_status()
    print(f"[IMPORT] completed in {time.time()-t0:.0f}s -> {r.json()}")


# ---------------------------------------------------------------------------
# RETRIEVAL PHASE (gateway)
# ---------------------------------------------------------------------------

def fetch_catalog_services():
    r = requests.get(f"{GATEWAY_URL}/services", timeout=120)
    r.raise_for_status()
    return json.loads(r.text)


def build_expected_map(queries):
    """resource_seg -> service_id, considering ONLY the smart-city mocks
    (services without sample_rank). This way the distractors do not steal a segment."""
    services = fetch_catalog_services()
    seg_to_id = {}
    for s in services:
        if s.get("sample_rank") is not None:
            continue  # apis.guru distractor → excluded from the expected map
        sid = s.get("_id")
        for op in (s.get("capabilities", {}) or {}).keys():
            seg = _resource_seg(_norm_op(op))
            if seg and seg not in seg_to_id:
                seg_to_id[seg] = sid
    expected, missing = {}, set()
    for row in queries:
        seg = row["seg"]
        if seg in seg_to_id:
            expected[seg] = seg_to_id[seg]
        elif seg:
            missing.add(seg)
    if missing:
        print(f"[WARN] Segments without a smart-city service: {sorted(missing)}")
    return expected


def build_smartcity_ids():
    """Set of the _id values of the smart-city services (without sample_rank). Everything else
    in the catalog is an apis.guru distractor. Used to measure intrusion."""
    return {s.get("_id") for s in fetch_catalog_services() if s.get("sample_rank") is None}


def gateway_search(query, max_rank=None):
    payload = {"query": query}
    if max_rank is not None:
        payload["max_rank"] = max_rank
    r = requests.post(f"{GATEWAY_URL}/index/search", json=payload, timeout=GATEWAY_TIMEOUT)
    r.raise_for_status()
    return r.json().get("results", [])


def _caps_dict_getter(svc):
    """Capability keys from a gateway result (capabilities = dict)."""
    return list((svc.get("capabilities", {}) or {}).keys())


def run_retrieval(queries, expected, smartcity_ids, max_rank=None):
    n = errors = coverage_ok = 0
    intruded = 0
    intruders = []

    for row in queries:
        steps  = row["oracle_steps"]
        exp_id = expected.get(row["seg"])
        if not steps or not exp_id:
            continue
        n += 1
        try:
            results = gateway_search(row["query"], max_rank=max_rank)
        except requests.exceptions.RequestException as e:
            errors += 1
            print(f"    [WARN] query failed ({type(e).__name__}): {row['query'][:50]}")
            continue

        ids = [s.get("_id") for s in results]

        # Find the EXPECTED service in the results and its position
        srank = next((i for i, svc in enumerate(results, 1) if svc.get("_id") == exp_id), None)

        # ── Distractor intrusion: how many distractors rank ABOVE the expected one ──
        cut = (srank - 1) if srank else len(ids)
        n_intruders = sum(1 for rid in ids[:cut] if rid not in smartcity_ids)
        intruders.append(n_intruders)
        if n_intruders > 0:
            intruded += 1

        # Cross-service COVERAGE: each oracle step present in ANY
        # returned service (the steps may live on different services).
        if all(_op_present_anywhere(results, _caps_dict_getter, m, p) for m, p in steps):
            coverage_ok += 1

    return {
        "ret_queries":      n,
        "ret_errors":       errors,
        "ep_coverage_rate": round(coverage_ok/n, 4) if n else 0.0,
        "intrusion_rate":   round(intruded/n, 4) if n else 0.0,
    }


# ---------------------------------------------------------------------------
# END-TO-END PHASE (control-unit)
# ---------------------------------------------------------------------------

def invoke_control_unit(query, max_rank=None):
    body = {"input": query}
    if max_rank is not None:
        body["max_rank"] = max_rank
    t0 = time.perf_counter()
    r  = requests.post(CONTROL_URL, json=body, timeout=TIMEOUT)
    return r.json(), time.perf_counter() - t0


def _caps_list_getter(svc):
    """Capability keys from a 'discovered' item (capabilities = list)."""
    return svc.get("capabilities", []) or []


def run_control(queries, expected, smartcity_ids, max_rank=None):
    n = len(queries)
    l1_pass = l1_partial = l1_fail = final_correct = errors = 0
    tp = fp = fn = 0
    latencies = []
    # Retrieval metrics derived from 'discovered' (what the LLM saw,
    # post registry filter). Independent of the standalone retrieval phase.
    # NB: no coverage here - end-to-end is misleading because the LLM may
    # complete endpoints of a service it sees but whose operations were
    # pruned from the capability list. The reliable metrics are L1.
    c_ret_n = c_intruded = 0
    c_intruders = []

    for i, row in enumerate(queries, 1):
        try:
            data, latency = invoke_control_unit(row["query"], max_rank=max_rank)
        except requests.exceptions.RequestException as e:
            print(f"   [{i}/{n}] NETWORK ERROR: {e}")
            errors += 1
            continue
        latencies.append(latency)
        plan  = data.get("execution_plan", {}) or {}
        tasks = plan.get("tasks", []) if isinstance(plan, dict) else []
        l1 = TR.check_layer1_plan(tasks, row["oracle_steps"])
        if l1["verdict"] == "PASS":      l1_pass += 1
        elif l1["verdict"] == "PARTIAL": l1_partial += 1
        else:                            l1_fail += 1
        tp += l1["tp"]; fp += l1["fp"]; fn += l1["fn"]
        ev = TR.evaluate_all_layers(data, row["oracle_steps"], tasks)
        if ev["final"] == "CORRECT":
            final_correct += 1

        # ── Retrieval seen by the LLM ('discovered' field of the response) ──
        steps  = row["oracle_steps"]
        exp_id = expected.get(row["seg"])
        if steps and exp_id:
            c_ret_n += 1
            discovered = data.get("discovered", []) or []
            d_ids = [s.get("_id") for s in discovered]
            srank = next((j for j, rid in enumerate(d_ids, 1) if rid == exp_id), None)

            # Intrusion: distractors above the expected one (or all, if absent)
            cut = (srank - 1) if srank else len(d_ids)
            n_intr = sum(1 for rid in d_ids[:cut] if rid not in smartcity_ids)
            c_intruders.append(n_intr)
            if n_intr > 0:
                c_intruded += 1

        chosen = [f"{t.get('operation','')} {t.get('url') or t.get('endpoint','')}" for t in tasks]
        print(f"   [{i}/{n}] L1={l1['verdict']:<7} final={ev['final']:<9} "
              f"F1={l1['f1']:.2f} ⏱{latency:.1f}s | {chosen}")
        time.sleep(DELAY)
    micro_p  = tp/(tp+fp) if (tp+fp) else 0.0
    micro_r  = tp/(tp+fn) if (tp+fn) else 0.0
    micro_f1 = 2*micro_p*micro_r/(micro_p+micro_r) if (micro_p+micro_r) else 0.0
    evaluated = n - errors
    return {
        "ctrl_queries":            n,
        "ctrl_errors":             errors,
        "ctrl_l1_pass":            l1_pass,
        "ctrl_l1_partial":         l1_partial,
        "ctrl_l1_fail":            l1_fail,
        "ctrl_selection_accuracy": round(l1_pass/evaluated, 4) if evaluated else 0.0,
        "ctrl_micro_f1":           round(micro_f1, 4),
        "ctrl_final_correct_rate": round(final_correct/evaluated, 4) if evaluated else 0.0,
        "ctrl_mean_latency_s":     round(sum(latencies)/len(latencies), 2) if latencies else "",
        # Retrieval seen by the LLM (from 'discovered', post registry filter)
        "cret_queries":            c_ret_n,
        "cret_intrusion_rate":     round(c_intruded/c_ret_n, 4) if c_ret_n else 0.0,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def write_results(results):
    fields = ["distractors"]
    for r in results:
        for k in r:
            if k not in fields:
                fields.append(k)
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(results)


def main():
    if SCOPE_MODE not in ("import", "filter"):
        sys.exit(f"[ERROR] invalid SCOPE_MODE: '{SCOPE_MODE}' (expected 'import' or 'filter')")

    print(f"Mode         : {SCOPE_MODE}")
    print(f"Phases       : {sorted(PHASES)}")
    print(f"Gateway      : {GATEWAY_URL}")
    print(f"Control-unit : {CONTROL_URL}")
    print(f"Importer     : {IMPORTER_URL}")
    print(f"CSV          : {REQUESTS_CSV}")
    print(f"Steps        : {MIN_DISTRACTORS} -> {MAX_DISTRACTORS} step {STEP} | seed={SEED}")
    if SCOPE_MODE == "filter":
        print("               (no import: uses max_rank on the 200 already tagged by backfill_ranks)")
    if QUERY_FILTER != "all":
        print(f"Query filter : {QUERY_FILTER}")
    if MAX_QUERIES:
        print(f"Subset       : first {MAX_QUERIES} (after filtering)")
    print()

    queries = load_queries(REQUESTS_CSV)
    print(f"[INIT] {len(queries)} queries loaded"
          + (f" (filter={QUERY_FILTER})" if QUERY_FILTER != "all" else "") + ".")
    if QUERY_FILTER == "cross" and len(queries) < MAX_QUERIES:
        print(f"[WARN] only {len(queries)} cross-service queries available "
              f"(requested {MAX_QUERIES}). Consider a CSV with more cross-service queries.")

    # The expected map is used by both phases (direct retrieval and
    # retrieval-from-control), so we always build it.
    expected = build_expected_map(queries)
    smartcity_ids = build_smartcity_ids()
    print(f"[INIT] {len(expected)} expected services mapped ; "
          f"{len(smartcity_ids)} smart-city services (the rest = distractors).")
    print()

    levels = list(range(MIN_DISTRACTORS, MAX_DISTRACTORS + 1, STEP))
    if not levels or levels[-1] != MAX_DISTRACTORS:
        levels.append(MAX_DISTRACTORS)

    results = []
    prev = 0
    for level in levels:
        # In import mode the distractors are loaded; in filter mode max_rank is used.
        scope = None
        if SCOPE_MODE == "import":
            if level > prev:
                import_distractors(level, offset=prev)
                prev = level
        else:  # filter
            scope = level

        print(f"\n=== Distractor level: {level} ===")
        row = {"distractors": level}

        if "retrieval" in PHASES:
            print("  [RETRIEVAL]")
            row.update(run_retrieval(queries, expected, smartcity_ids, max_rank=scope))
            print(f"    coverage={row['ep_coverage_rate']}  |  "
                  f"distractor intrusion: {row['intrusion_rate']}")

        if "control" in PHASES:
            print("  [CONTROL-UNIT]")
            row.update(run_control(queries, expected, smartcity_ids, max_rank=scope))
            print(f"    RETRIEVAL(LLM) distractor intrusion={row['cret_intrusion_rate']}")
            print(f"    selection_accuracy={row['ctrl_selection_accuracy']} "
                  f"micro_f1={row['ctrl_micro_f1']} "
                  f"final_correct_rate={row['ctrl_final_correct_rate']} "
                  f"(errors={row['ctrl_errors']})")

        results.append(row)
        write_results(results)
        print(f"    [SAVE] {OUT_CSV} updated ({len(results)} levels)")

    print(f"\n✅ Results saved to {OUT_CSV}")
    print("\nRobustness summary vs number of distractors:")
    header = "  dist"
    if "retrieval" in PHASES:
        if not CROSS_ONLY:
            header += "  svc@1  svc@5  ep@1   ep@5"
        header += "  ep_cov  intr"
    if "control" in PHASES:
        if not CROSS_ONLY:
            header += "  | Lep@1  Lep@5"
        header += "  | Lintr   sel_acc  micro_f1  final_ok"
    print(header)
    print("  (ep = gateway retrieval ; cov = coverage ; intr = intrusion_rate ; "
          "L* = retrieval seen by the LLM ; sel/final = end-to-end)")
    for r in results:
        line = f"  {r['distractors']:>4}"
        if "retrieval" in PHASES:
            line += f"  {r['ep_coverage_rate']:>5}  {r['intrusion_rate']:>5}"
        if "control" in PHASES:
            line += (f"  | {r['cret_intrusion_rate']:>5}"
                     f"  {r['ctrl_selection_accuracy']:>7}  {r['ctrl_micro_f1']:>8}  {r['ctrl_final_correct_rate']:>8}")
        print(line)


if __name__ == "__main__":
    try:
        main()
    except requests.exceptions.RequestException as e:
        print(f"[HTTP ERROR] {e}", file=sys.stderr)
        sys.exit(1)
