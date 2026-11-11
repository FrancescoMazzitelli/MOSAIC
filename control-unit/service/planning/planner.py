"""Control Unit Planner.

This module contains the `Planner` class, which turns a user query
plus the information about the discovered services into a JSON execution plan.

The planner coordinates prompts, schemas and the LLM, then validates and repairs the plan.
The actual execution of the tasks happens in another component.
"""

import json
import os
import re
from urllib.parse import urlparse, parse_qs

from service.designerService import Designer
from service.planning.llm_client import LLMClient
from service.planning.prompt_provider import PromptProvider
from service.planning.schema_provider import SchemaProvider


class Planner:
    """Planning phase: from query + discovered services to a JSON plan.

    `Planner` builds the system prompt and the user prompt, invokes the LLM, extracts
    the generated JSON, validates the tasks and applies any auto-fix.

    It does not perform HTTP/SQL calls: it only produces and checks the
    plan. The fallback for empty plans is delegated to `Designer`.
    """

    def __init__(self, model_name: str, prompts: PromptProvider = None,
                 schemas: SchemaProvider = None, llm: LLMClient = None,
                 designer: Designer = None):
        self.model_name  = model_name
        self.prompts     = prompts  or PromptProvider()
        self.schemas     = schemas  or SchemaProvider()
        self.llm         = llm      or LLMClient(model_name)
        self.designer    = designer or Designer(fallback_model=model_name)
        self.backend_mode = "MOCK"

    # -- public pipeline ---------------------------------------------------
    def plan(self, query, discovered: dict, input_files=None,
             backend_mode: str = "MOCK") -> tuple[dict, float, list]:
        """Generates an execution plan from the query and the catalog.

        Args:
            query (str): text of the user's request.
            discovered (dict): information about discovered services, endpoints and schemas.
            input_files (any, optional): input files to include in the request.
            backend_mode (str): 'MOCK' or 'REAL', used to build the prompt.

        Returns:
            tuple[dict, float, list]: generated plan, rounded LLM latency,
                list of schema validation warnings.
        """
        self.backend_mode = backend_mode
        system = self.prompts.system_prompt(backend_mode)
        user   = self.prompts.user_prompt(
            discovered["services"], discovered["capabilities"], discovered["endpoints"],
            discovered["response_schemas"], discovered["request_schemas"],
            discovered["parameters"], query, input_files)
        input_schema  = self.schemas.input_schema(discovered["request_schemas"])
        output_schema = self.schemas.output_schema()

        raw_resp, latency = self.llm.complete(system, user, output_schema, input_schema)
        print(f"[LLM RESPONSE] {raw_resp}")
        print("=" * 100)
        print(f"[LATENCY] Plan generated in {latency:.2f}s")

        plan = self.extract_agents(raw_resp)
        warnings = self._validate_plan(
            plan, discovered["services"], discovered["request_schemas"],
            discovered["parameters"], backend_mode)
        return plan, round(latency, 3), warnings

    def is_empty(self, plan: dict) -> bool:
        """Checks whether the generated plan is empty (no executable task)."""
        return self._empty_plan_detected(plan)

    def diagnose_empty(self, query, plan, discovered: dict, input_files=None) -> dict:
        """Runs the triage of an empty plan through the Designer.

        The Designer will assess whether the problem is out-of-domain, ambiguous, or requires an
        alternative service contract.
        """
        return self.designer.analyze(
            query=query,
            plan_reasoning=plan.get("reasoning", ""),
            discovered_services=discovered["services"],
            discovered_capabilities=discovered["capabilities"],
            input_files=input_files,
        )

    def repair(self, plan: dict, available_ids=None, name_to_id=None) -> dict:
        """Attempts to automatically repair the plan before execution.

        Fixes known problems such as wrong service_ids and badly formatted mock URLs.
        """
        return self._attempt_auto_fix(plan, available_ids, name_to_id)

    # -- methods moved 1:1 from the old Controller -------------------------
    def extract_agents(self, agents_json: str) -> dict:
        """Extracts valid JSON from the LLM's text response.

        Supports three strategies:
          1. direct parse,
          2. parse after a </think> tag,
          3. raw search for the first valid JSON object.
        """
        def try_parse(text):
            s, e = text.find('{'), text.rfind('}') + 1
            if s != -1 and e > s:
                try:
                    return json.loads(text[s:e])
                except json.JSONDecodeError:
                    pass
            return None

        # Level 1: direct parse (always the case with structured output)
        try:
            result = json.loads(agents_json)
            if isinstance(result, dict):
                print("[PARSE] Direct.")
                return result
        except json.JSONDecodeError:
            pass

        # Level 2: after </think> (fallback for reasoning models)
        m = re.search(r'</think>', agents_json, flags=re.IGNORECASE)
        if m:
            result = try_parse(agents_json[m.end():].strip())
            if result is not None:
                print("[PARSE] After </think>.")
                return result

        # Level 3: raw search
        result = try_parse(agents_json)
        if result is not None:
            print("[PARSE] Raw.")
            return result

        print(f"[FORMAT ERROR] No valid JSON. Preview: {agents_json[:200]}")
        return {}

    def _empty_plan_detected(self, plan: dict) -> bool:
        """
        Detects a plan with no executable tasks. The CLASSIFICATION of the reason
        (out-of-domain, ambiguous, invalid, ...) is delegated to
        Designer.analyze() which makes a single LLM call and also decides whether
        to design a contract or not.

        Condition: tasks is an empty list. A malformed plan ({}) has
        no tasks and does not trigger the fallback.
        """
        if not isinstance(plan, dict):
            return False
        tasks = plan.get("tasks")
        return isinstance(tasks, list) and len(tasks) == 0

    def _validate_plan(self,
                       plan: dict,
                       discovered_services: list,
                       discovered_request_schemas: list,
                       discovered_parameters: list | None = None,
                       backend_mode: str = "MOCK") -> list[str]:
        """
        Post-parse validator for the plan generated by the LLM.

        Checks performed for each task:
          1. (POST/PUT/PATCH) The keys of the 'input' body must be documented
             in the request_schema of the called endpoint.
          2. (GET) Every query param in the URL must be in the set of parameters
             documented by the catalog for that endpoint.
          3. (GET, HC-12, MOCK only) A URL must not combine two or more
             query params in AND: mock servers (Microcks) match one parameter
             at a time against the registered examples. In REAL the check is
             skipped because real APIs support AND-combination.

        Returns a list of warnings: an empty list means a clean plan.

        Useful for:
          - Logging residual violations that guided decoding did not block
          - Collecting quantitative data for the thesis (% violations pre/post patch)
          - Extending in the future with selective auto-retry on the single violated task
        """
        warnings: list[str] = []
        field_pattern = re.compile(r'(\w+):')

        # Build two maps  path_endpoint → set_of_valid_names:
        #   endpoint_valid_keys   → body keys (POST/PUT)
        #   endpoint_valid_params → query param names (GET)
        endpoint_valid_keys:   dict[str, set] = {}
        endpoint_valid_params: dict[str, set] = {}

        for i, _ in enumerate(discovered_services):
            schemas = discovered_request_schemas[i] \
                      if i < len(discovered_request_schemas) else {}
            params  = discovered_parameters[i] \
                      if discovered_parameters and i < len(discovered_parameters) else {}

            for ep_key, schema_str in schemas.items():
                if not schema_str:
                    continue
                path = ep_key.split(" ")[-1]          # "POST /sort" → "/sort"
                endpoint_valid_keys[path] = set(field_pattern.findall(schema_str))

            for ep_key, params_str in params.items():
                if not params_str:
                    continue
                path = ep_key.split(" ")[-1]          # "GET /sensor" → "/sensor"
                endpoint_valid_params[path] = set(field_pattern.findall(str(params_str)))

        for task in plan.get("tasks", []):
            task_name = task.get("task_name", "?")
            url       = task.get("url", "")
            input_val = task.get("input")
            operation = str(task.get("operation") or "GET").upper()

            # ── Check 1: body keys (POST/PUT/PATCH) ──────────────────────────
            if isinstance(input_val, dict) and input_val:
                matched_valid_keys = None
                for ep_path, valid_keys in endpoint_valid_keys.items():
                    if ep_path in url:
                        matched_valid_keys = valid_keys
                        break

                if matched_valid_keys is not None:
                    hallucinated = set(input_val.keys()) - matched_valid_keys
                    if hallucinated:
                        warnings.append(
                            f"[SCHEMA VIOLATION] Task '{task_name}' → "
                            f"invalid keys: {sorted(hallucinated)} | "
                            f"allowed keys: {sorted(matched_valid_keys)}"
                        )

            # ── Check 2 & 3: GET query params (HC-12) ────────────────────────
            if operation == "GET" and url:
                try:
                    parsed = urlparse(url)
                    qs = parse_qs(parsed.query, keep_blank_values=True)
                except Exception:
                    qs = {}

                param_names = set(qs.keys())

                # Match endpoint on the path (same heuristic as check 1)
                matched_valid_params = None
                for ep_path, valid_names in endpoint_valid_params.items():
                    if ep_path in parsed.path:
                        matched_valid_params = valid_names
                        break

                # Check 2: params not documented in the catalog
                if matched_valid_params:   # non-None and non-empty
                    hallucinated_params = param_names - matched_valid_params
                    if hallucinated_params:
                        warnings.append(
                            f"[PARAM VIOLATION] Task '{task_name}' → "
                            f"query params not documented: {sorted(hallucinated_params)} | "
                            f"documented: {sorted(matched_valid_params)}"
                        )

                # Check 3: HC-12 — AND combination (MOCK only)
                if backend_mode == "MOCK" and len(param_names) >= 2:
                    warnings.append(
                        f"[HC-12 VIOLATION] Task '{task_name}' → "
                        f"GET combines {len(param_names)} query params with AND: "
                        f"{sorted(param_names)} | "
                        f"url: {url[:120]}"
                    )

        return warnings

    def _attempt_auto_fix(self, plan: dict, available_ids: list = None, name_to_id: dict = None) -> dict:
        mock_url = os.environ.get("MOCK_SERVER_URL", "http://mock-server:8080")
        id_set   = set(available_ids or [])
        fixed_tasks = []
        for task in plan.get("tasks", []):
            if not isinstance(task, dict):
                continue

            # ── Fix service_id: model used name instead of _id ───────────
            sid = task.get("service_id", "")
            if sid and sid not in id_set and name_to_id and sid in name_to_id:
                corrected = name_to_id[sid]
                print(f"[AUTO-FIX] service_id '{sid}' → '{corrected}'")
                task["service_id"] = corrected

            url = str(task.get("url", ""))
            # Mock wiring (localhost rewrite and mock_url prepend) applies
            # only in MOCK. In REAL a non-http or localhost URL is a planning
            # bug: it will be discarded by the subsequent validation.
            if self.backend_mode == "MOCK":
                # 1. Replace localhost with the mock server address
                if url and re.search(r'http://localhost:\d+', url):
                    fixed = re.sub(r'http://localhost:\d+', mock_url, url)
                    print(f"[AUTO-FIX] localhost → mock-server: {fixed}")
                    task["url"] = fixed
                    url = fixed

                # 2. Strip angle brackets  <https://...>  →  https://...
                #    Model's Markdown formatting artifact (not a
                #    planning error): the model knows the URL but
                #    wrapped it with the Markdown link < > symbols.
                if url.startswith('<') and url.endswith('>'):
                    url = url[1:-1]
                    task["url"] = url
                    print(f"[AUTO-FIX] Stripped angle brackets: {url}")

                # 3. URL without scheme → prepend mock_url
                if url and not url.startswith("http") and not url.startswith("{{"):
                    task["url"] = (mock_url if url.startswith("/") else mock_url + "/") + url.lstrip("/")
                    print(f"[AUTO-FIX] URL: {task['url']}")

            if isinstance(task.get("operation"), str):
                task["operation"] = task["operation"].upper()
            if all(f in task for f in ("task_name", "service_id", "url", "operation")):
                fixed_tasks.append(task)
            else:
                print(f"[AUTO-FIX] Task discarded: {task.get('task_name', 'unnamed')}")
        plan["tasks"] = fixed_tasks
        return plan
