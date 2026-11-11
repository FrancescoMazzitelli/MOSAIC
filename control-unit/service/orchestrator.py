"""Control Unit Orchestrator.

This module defines the `Orchestrator` class, which coordinates the
control unit's main pipeline: discovery -> planning -> execution.

The main responsibilities of the `Orchestrator` are:
- querying the catalog to find services relevant to a query;
- filtering the services actually registered in the registry;
- delegating plan generation to the `Planner`;
- validating and, where possible, repairing the plan with `PlanValidator`;
- executing the plan through the `Executor` and returning the results.

The responses of the `control()` method are structured as dictionaries with
keys such as `execution_plan`, `execution_results`, `error` and
`planning_latency_s`. If the result is a file to download,
a `flask.Response` object is returned.
"""

import os
import shutil
import mimetypes
import requests
import re
from flask import Response

from service.discoveryService import Discovery
from service.planning.planner import Planner
from service.planning.plan_validator import PlanValidator
from service.execution.executor import Executor



class Orchestrator:
    """Coordinates discovery, planning and execution.

    Main attributes:
    - `model_name` (str): name of the LLM model used by the `Planner`.
    - `backend_mode` (str): backend mode ('MOCK' or 'REAL').
    - `planner` (Planner): instance responsible for generating plans.
    - `executor` (Executor): instance responsible for executing the plan.

    The constructor also creates a `Files` folder for temporarily
    storing files uploaded by the user.
    """

    def __init__(self):
        self.model_name = os.environ.get("LLM_MODEL", "qwen3.5:27b")
        self.backend_mode = "MOCK"
        self.planner = Planner(self.model_name)
        self.executor = Executor()
        os.makedirs("Files", exist_ok=True)

    
    def analyze_files(self, files: list):
        """Analyzes and saves files uploaded by the user.

        Each file is saved in the `Files` folder and a list of dictionaries
        with metadata useful to the `Planner` is returned.

        Args:
            files (list): list of file objects (e.g. from Flask `request.files`).

        Returns:
            list: list of dictionaries with keys `filename`, `content_type`,
                  `size`, `path`, `category`.
        """
        analyzed = []
        for f in files:
            filename = os.path.basename(f.filename)
            content_type = f.mimetype or mimetypes.guess_type(filename)[0]
            path = os.path.join("Files", filename)
            f.save(path)
            category = "unknown"
            if content_type == "application/pdf":
                category = "document"
            elif content_type and content_type.startswith("image/"):
                category = "image"
            elif content_type in ["text/csv", "application/vnd.ms-excel"]:
                category = "tabular"
            analyzed.append({
                "filename": filename,
                "content_type": content_type,
                "size": f.content_length or 0,
                "path": path,
                "category": category,
            })
        return analyzed

    def replace_endpoints(self, endpoints_list, mock_server_address):
        """Replaces mock URLs when in `MOCK` mode.

        Some services registered in the catalog may point to
        `http://localhost:8585` (Microcks configuration). When the orchestrator
        runs in a container, this function rewrites those URLs with
        `mock_server_address` so that the mocks are reachable.

        Args:
            endpoints_list (list): list of dictionaries with endpoints.
            mock_server_address (str): address of the mock server to use.

        Returns:
            list: list of endpoints with URLs possibly rewritten.
        """
        if self.backend_mode == "REAL":
            return endpoints_list
        return [
            {k: re.sub(r"http://localhost:8585", mock_server_address, v)
             if isinstance(v, str) else v
             for k, v in ep.items()}
            for ep in endpoints_list
        ]

    # -- ENTRY POINT (ex Controller.control) -----------------
    def control(self, query, files=None, max_rank=None, dry_run=False):
        """Main entry point for request orchestration.

        General flow:
        1. Analyze any uploaded files.
        2. Retrieve services from the catalog and filter the registered ones.
        3. Call the `Planner` to obtain an execution plan.
        4. Validate (and attempt to repair) the plan.
        5. Send the plan to the `Executor` and collect the results.

        Args:
            query (str): description of the user's request.
            files (list, optional): list of files uploaded by the user.
            max_rank (int, optional): limits the services returned by the catalog.
            dry_run (bool): if True, stops after planning/validation and does not
                execute the plan. Used by the Designer evaluation harness.

        Returns:
            dict | flask.Response: dictionary with `execution_plan`,
                `execution_results`, `error`, `planning_latency_s`, or a
                `flask.Response` if the result is a file to download.
        """

        # Save metadata of uploaded files (if any) for the planner
        analyzed_files = self.analyze_files(files or [])
        planning_latency_s = None

        # Routing metadata used by the evaluation harness. They are additive
        # and do not change the system's behavior.
        route_reason = "UNKNOWN"
        planner_status = None
        planner_tasks_count = None
        validated_tasks_count = None
        designer_attempted = False

        # Determine the backend mode (MOCK or REAL)
        self.backend_mode = os.environ.get("BACKEND_MODE", "MOCK").upper()
        if self.backend_mode not in ("MOCK", "REAL"):
            # If the value is not recognized, fall back to MOCK for safety
            print(f"[BACKEND_MODE] unrecognized value '{self.backend_mode}', falling back to MOCK")
            self.backend_mode = "MOCK"
        print(f"[BACKEND_MODE] {self.backend_mode}")

        # External configuration: URLs of the catalog/registry/mock services
        catalog_url  = os.environ.get("CATALOG_URL",     "http://catalog-gateway:5000")
        registry_url = os.environ.get("REGISTRY_URL",    "http://registry:8500")
        mock_url     = os.environ.get("MOCK_SERVER_URL", "http://mock-server:8080")

        # Discovery: get the list of services from the registry
        registry     = Discovery(registry_url)
        services     = registry.services()
        # Query the catalog to find services relevant to the query.
        # max_rank (if present) limits the distractors considered — used by the
        # robustness test to simulate a catalog with N distractors.
        search_payload = {"query": query}
        if max_rank is not None:
            search_payload["max_rank"] = max_rank
        service_data = requests.post(f"{catalog_url}/index/search", json=search_payload).json()
        service_list = service_data.get("results", [])

        # No service found -> explicit error response
        if not service_list:
            return {"execution_plan": {}, "execution_results": [],
                    "error": "No services matched the query",
                    "discovered": [],
                    "route_reason": "NO_SERVICES",
                    "planner_status": None,
                    "planner_tasks_count": None,
                    "validated_tasks_count": None,
                    "designer_attempted": False,
                    "dry_run": bool(dry_run),
                    "planning_latency_s": planning_latency_s}

        # Keep only the services actually registered in the registry
        registry_ids          = {s["id"] for s in services}
        filtered_service_list = [s for s in service_list if s["_id"] in registry_ids]
        orphaned              = [s for s in service_list if s["_id"] not in registry_ids]
        if orphaned:
            # Log services present in the catalog but not in the registry
            print("[WARNING] Services no longer in the registry:", [s.get("_id") for s in orphaned])
        if not filtered_service_list:
            return {"execution_plan": {}, "execution_results": [],
                    "error": "None of the discovered services are currently available",
                    "discovered": [],
                    "route_reason": "REGISTRY_EMPTY",
                    "planner_status": None,
                    "planner_tasks_count": None,
                    "validated_tasks_count": None,
                    "designer_attempted": False,
                    "dry_run": bool(dry_run),
                    "planning_latency_s": planning_latency_s}

        # Detailed output for debug/monitoring
        print("\n" + "=" * 60)
        print(f"[RETRIEVAL] Query: {query}")
        for s in filtered_service_list:
            caps = s.get("capabilities", {})
            print(f"  - {s.get('_id')} | {s.get('name')} | {len(caps)} endpoints")
            print(f"    {list(caps.keys())}")
        print("=" * 60 + "\n")

        # Normalize and collect the useful information from the catalog
        disc_services, disc_caps, disc_eps = [], [], []
        disc_schemas, disc_req_schemas, disc_params = [], [], []
        for s in filtered_service_list:
            # Remove the internal registration endpoint if present
            if isinstance(s.get("capabilities"), dict): s["capabilities"].pop("POST /register", None)
            if isinstance(s.get("endpoints"), dict):    s["endpoints"].pop("POST /register", None)
            disc_services.append({"_id": s.get("_id"), "name": s.get("name"),
                                  "description": s.get("description")})
            disc_caps.append(s.get("capabilities", {}))
            disc_eps.append(s.get("endpoints", {})) 
            disc_schemas.append(s.get("response_schemas", {}))
            disc_req_schemas.append(s.get("request_schemas", {}))
            disc_params.append(s.get("parameters", {}))

        # Rewrite the mock endpoints when needed with the mock server address reachable from the containers
        disc_eps = self.replace_endpoints(disc_eps, mock_url)

        # Overall data structure to pass to the planner
        discovered = {
            "services": disc_services, "capabilities": disc_caps, "endpoints": disc_eps,
            "response_schemas": disc_schemas, "request_schemas": disc_req_schemas,
            "parameters": disc_params,
        }

        # Compact view of the retrieved services (ordered as from retrieval, already
        # filtered by registry): id + capability. Exposed in the response so the
        # test can compute retrieval metrics on what the LLM saw,
        # without having to scrape the logs.
        discovered_out = [
            {"_id": disc_services[i]["_id"],
             "name": disc_services[i]["name"],
             "capabilities": list((disc_caps[i] or {}).keys())}
            for i in range(len(disc_services))
        ]

        # ---- PLANNING ----
        # Ask the planner to build a plan from the query
        plan, planning_latency_s, schema_warnings = self.planner.plan(
            query, discovered, analyzed_files, self.backend_mode)

        # Report any schema violations detected by the validator
        if schema_warnings:
            print(f"\n{'WARN ' * 10}")
            print(f"[SCHEMA VALIDATOR] {len(schema_warnings)} violation(s) detected:")
            for w in schema_warnings:
                print(f"  {w}")
            print(f"{'WARN ' * 10}\n")
        else:
            print("[SCHEMA VALIDATOR] No violations detected.")

        # Planner status, independent of what happens after
        # validation. Used by the harness to distinguish a non-empty plan from
        # malformed JSON or from a plan emptied during auto-fix.
        if isinstance(plan, dict) and isinstance(plan.get("tasks"), list):
            planner_tasks_count = len(plan["tasks"])
            planner_status = "EMPTY" if planner_tasks_count == 0 else "NONEMPTY"
        else:
            planner_status = "MALFORMED"

        if planner_status == "EMPTY":
            route_reason = "PLANNER_EMPTY_DESIGNER"
        elif planner_status == "MALFORMED":
            route_reason = "PLANNER_MALFORMED"
        else:
            route_reason = "PLANNER_NONEMPTY"

        # ---- EMPTY PLAN -> Designer (triage + design) ----
        # If the planner proposes no action, activate the design module
        if self.planner.is_empty(plan):
            print("\n[ROUTING] Empty plan detected. Activating Designer (triage + design)...")
            designer_attempted = True
            analysis = self.planner.diagnose_empty(query, plan, discovered, analyzed_files)
            category = analysis.get("category", "UNKNOWN")
            contract = analysis.get("service_contract")
            if category == "OUT_OF_DOMAIN" and isinstance(contract, dict):
                error_msg = ("Query out of domain. The system requires "
                             "services not currently available.")
            else:
                error_msg = (f"Empty plan classified as {category}: "
                             f"{analysis.get('justification', '')}")
            return {
                "execution_plan":           plan,
                "execution_results":        [],
                "error":                    error_msg,
                "empty_plan_category":      category,
                "empty_plan_justification": analysis.get("justification", ""),
                "suggested_api_contracts":  [contract] if isinstance(contract, dict) else [],
                "discovered":               discovered_out,
                "planning_latency_s":       planning_latency_s,
                "route_reason":             "PLANNER_EMPTY_DESIGNER",
                "planner_status":           "EMPTY",
                "planner_tasks_count":      0,
                "validated_tasks_count":    0,
                "designer_attempted":       designer_attempted,
                "dry_run":                  bool(dry_run),
            }

        # ---- VALIDATION + AUTO-FIX ----
        # Validate the plan against the services actually available
        available_ids = [s["_id"] for s in disc_services] # List of available service IDs (after registry filtering)
        name_to_id    = {s.get("name"): s.get("_id") for s in disc_services} # name->ID mapping for possible auto-fix
        is_valid, val_errors = PlanValidator.validate(plan, available_ids)
        if not is_valid:
            print(f"[VALIDATION] {len(val_errors)} error(s):")
            for e in val_errors:
                print(f"  - {e}")
            # Attempt automatic plan repair
            plan = self.planner.repair(plan, available_ids, name_to_id)
            is_valid, val_errors = PlanValidator.validate(plan, available_ids)
            if not is_valid:
                # If repair is not possible, cancel the tasks
                print("[VALIDATION] Plan cannot be recovered - execution cancelled.")
                plan["tasks"] = []

        validated_tasks_count = (
            len(plan.get("tasks", []))
            if isinstance(plan, dict) and isinstance(plan.get("tasks"), list)
            else 0
        )
        if planner_status == "NONEMPTY" and validated_tasks_count == 0:
            route_reason = "POST_VALIDATION_EMPTY"

        # Dry-run: stop after planning + validation. Useful for the Designer
        # harness, which must not execute potentially write-side plans.
        if dry_run:
            return {
                "execution_plan":        plan,
                "execution_results":     [],
                "error":                 None,
                "discovered":            discovered_out,
                "planning_latency_s":    planning_latency_s,
                "route_reason":          route_reason,
                "planner_status":        planner_status,
                "planner_tasks_count":   planner_tasks_count,
                "validated_tasks_count": validated_tasks_count,
                "designer_attempted":    designer_attempted,
                "dry_run":               True,
            }

        # ---- EXECUTION ----
        # Execute the plan and collect the results
        results = self.executor.run(plan, disc_services, self.backend_mode)

        # If the executor returns a file, return it as a Response
        if isinstance(results, dict) and results.get("status") == "FILE":
            return Response(results["body"], status=results["status_code"],
                            headers=results["headers"])

        # Clean up the temporary files saved in `Files`
        if os.path.exists("Files"):
            for filename in os.listdir("Files"):
                file_path = os.path.join("Files", filename)
                try:
                    if os.path.isfile(file_path): os.unlink(file_path)
                    elif os.path.isdir(file_path): shutil.rmtree(file_path)
                except Exception as e:
                    # Do not block execution on a cleanup error
                    print(f"[WARN] Cleanup failed: {e}")

        # Final response containing the plan and the execution results
        return {
            "execution_plan":        plan,
            "execution_results":     results,
            "discovered":            discovered_out,
            "planning_latency_s":    planning_latency_s,
            "route_reason":          route_reason,
            "planner_status":        planner_status,
            "planner_tasks_count":   planner_tasks_count,
            "validated_tasks_count": validated_tasks_count,
            "designer_attempted":    designer_attempted,
            "dry_run":               False,
        }
