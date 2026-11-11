"""Provider of the system and user prompts used by the LLM planner.

This module builds the system prompt and the user prompt required by the
planner. It supports:
- an external system prompt template file via
  `PLANNER_SYSTEM_PROMPT_PATH`,
- fallback logic in Python with sentinels to handle the
  `MOCK` and `REAL` modes,
- generation of a user prompt describing services, endpoints,
  schemas and the user query.

The system prompt template uses special sentinels (`@@HC12_BLOCK@@` and
`@@HC12_SELFCHECK@@`) to inject mode-specific rules.
"""

import json
import os
import sys
from pathlib import Path


# Fragments injected in Python based on BACKEND_MODE (as in the old
# _build_system_prompt). They stay in the code: a single external prompt file,
# with two sentinels, covers both MOCK and REAL.
_HC12_BLOCK = """

HC-12  NO AND-COMBINATION OF QUERY PARAMS
      A GET url MUST NOT carry two or more query parameters in AND (e.g. ?a=x&b=y)
      unless the endpoint's description EXPLICITLY documents that the combination
      is supported.
      Why: mock servers (Microcks) match each parameter in isolation against
      registered examples. AND-combining two independently-documented parameters
      returns the first matched example, NOT the intersection — silently
      producing wrong data.
      Correct pattern when two independent filters are needed and no single
      endpoint documents them together: issue ONE GET per filter, combine
      the results in a terminal SQL task (e.g. JOIN on zoneId, or WHERE IN)."""

_SELFCHECK_MOCK = "  □ No GET url combines two or more query params in AND (HC-12) — use one GET per filter + a terminal SQL task."
_SELFCHECK_REAL = "  □ Every query param in a GET url is documented in the endpoint's parameters list (HC-1)."


class PromptProvider:
    """Provides the system and user prompts to the LLM planner.

    `PromptProvider` builds a system prompt and a user prompt consistent with the
    service catalog discovered by the system and with the backend mode.

    The system prompt can be read from an external file with `PLANNER_SYSTEM_PROMPT_PATH`
    or generated dynamically with `_build_template()`.
    The `backend_mode` is applied at the end via `_apply_mode()`.
    """

    def __init__(self):
        self.system_prompt_path = os.environ.get("PLANNER_SYSTEM_PROMPT_PATH")

    def system_prompt(self, backend_mode: str = "MOCK") -> str:
        """Returns the system prompt formatted for the selected mode.

        Args:
            backend_mode (str): 'MOCK' or 'REAL'. Determines which blocks and
                self-checks to include in the prompt.

        Returns:
            str: full text of the system prompt.
        """
        if self.system_prompt_path and Path(self.system_prompt_path).exists():
            template = Path(self.system_prompt_path).read_text(encoding="utf-8")
        else:
            template = self._build_template()
        return self._apply_mode(template, backend_mode)

    def _apply_mode(self, template: str, backend_mode: str) -> str:
        """Applies the prompt variants based on the backend mode.

        The function replaces the `@@HC12_BLOCK@@` and
        `@@HC12_SELFCHECK@@` sentinels with the correct blocks for MOCK or REAL.
        """
        # Handle HC-12 in Python: present in MOCK, absent in REAL.
        if backend_mode == "REAL":
            hc12, selfcheck = "", _SELFCHECK_REAL
        else:
            hc12, selfcheck = _HC12_BLOCK, _SELFCHECK_MOCK
        return (template.replace("@@HC12_BLOCK@@", hc12)
                        .replace("@@HC12_SELFCHECK@@", selfcheck))

    def user_prompt(self, discovered_services, discovered_capabilities,
                    discovered_endpoints, discovered_schemas,
                    discovered_request_schemas, discovered_parameters,
                    query, input_files=None) -> str:
        """Builds the complete user prompt for the planner.

        Args:
            discovered_services (list): information about the discovered services.
            discovered_capabilities (list): textual capabilities per service.
            discovered_endpoints (list): endpoint URLs per service.
            discovered_schemas (list): discovered response schemas.
            discovered_request_schemas (list): discovered request schemas.
            discovered_parameters (list): discovered endpoint parameters.
            query (str): the user query.
            input_files (any, optional): information about the uploaded files.

        Returns:
            str: full text of the user prompt.
        """
        return self._build_user_prompt(
            discovered_services, discovered_capabilities, discovered_endpoints,
            discovered_schemas, discovered_request_schemas, discovered_parameters,
            query, input_files)

    def _build_template(self) -> str:
        """Generates the default system prompt, including examples.

        The default prompt includes a set of exhaustive examples that explain
        to the model how to build valid execution plans based on the available
        services. Generation is based on a single template with sentinels
        replaced by `_apply_mode()`.
        """

        # HC-12 (MOCK only) and its self-check are injected by
        # _apply_mode() via the @@HC12_BLOCK@@ / @@HC12_SELFCHECK@@ sentinels.

        # ── EXAMPLES ─────────────────────────────────────────────────────────
        # Six examples, one per structurally distinct pattern family.
        # Each uses a domain that will NEVER appear in production queries.
        # Each ends with a WHY comment that explains the abstract principle
        # so the model generalises to new domains rather than imitating form.

        # ── PATTERN A: single GET with one enum filter ────────────────────────
        ex_a = {
            "reasoning": (
                "DECOMPOSE: available books | "
                "MAP: smart-library-mock / GET /book | "
                "CHAIN: none | "
                "COMBINE: single task | "
                "FILTER: status=available (one param, catalog enum) | "
                "VALIDATE: ✓"
            ),
            "tasks": [{
                "task_name":  "get_available_books",
                "service_id": "smart-library-mock",
                "url":        "http://mock-server:8080/rest/Smart+Library+Management+API/1.0/book?status=available",
                "operation":  "GET",
                "input":      ""
            }]
        }
        # WHY: when a single filter satisfies the query, one GET is enough.
        # Use the exact enum value documented in the parameter schema.

        # ── PATTERN B: GET list → JMESPath id extraction → PUT path param ────
        ex_b = {
            "reasoning": (
                "DECOMPOSE: find patient Rossi → set discharged | "
                "MAP: smart-hospital-mock / GET /patient + PUT /patient/{id} | "
                "CHAIN: PUT path ← get_all_patients.patients[?surname=='Rossi'] | [0].id | "
                "COMBINE: chain: discharge_patient consumes get_all_patients via JMESPath | "
                "FILTER: surname match via JMESPath, not query param | "
                "VALIDATE: ✓ id in path, ✓ no bare placeholders"
            ),
            "tasks": [
                {
                    "task_name":  "get_all_patients",
                    "service_id": "smart-hospital-mock",
                    "url":        "http://mock-server:8080/rest/Smart+Hospital+Management+API/1.0/patient",
                    "operation":  "GET",
                    "input":      ""
                },
                {
                    "task_name":  "discharge_patient",
                    "service_id": "smart-hospital-mock",
                    "url":        "http://mock-server:8080/rest/Smart+Hospital+Management+API/1.0/patient/{{get_all_patients.patients[?surname=='Rossi'] | [0].id}}",
                    "operation":  "PUT",
                    "input": {
                        "zoneId":    "Z-SUD",
                        "surname":   "Rossi",
                        "status":    "discharged",
                        "wardId":    12,
                        "updatedAt": "2025-09-25T14:00:00Z"
                    }
                }
            ]
        }
        # WHY: inject chained ids directly into the url path string.
        # Use t.array_key[?key=='val'] | [0].field — include the wrapper key (patients,
        # items, results…) between the task name and the filter.
        # Never add a redundant GET-by-id when the id is already in a prior result.

        # ── PATTERN C: GET with filter → collect all zoneIds → zoneIds join ──
        ex_c = {
            "reasoning": (
                "DECOMPOSE: canteens near occupied halls | "
                "MAP: smart-campus-mock / GET /lecture-hall + GET /canteen | "
                "CHAIN: canteen?zoneIds ← get_occupied_halls[*].zoneId | join(',',@) | "
                "COMBINE: chain: get_canteens_near_halls consumes get_occupied_halls via JMESPath | "
                "FILTER: lecture-hall → status=occupied; canteen → zoneIds param documented | "
                "VALIDATE: ✓ join on string field, ✓ zoneIds in catalog"
            ),
            "tasks": [
                {
                    "task_name":  "get_occupied_halls",
                    "service_id": "smart-campus-mock",
                    "url":        "http://mock-server:8080/rest/Smart+University+Campus+API/1.0/lecture-hall?status=occupied",
                    "operation":  "GET",
                    "input":      ""
                },
                {
                    "task_name":  "get_canteens_near_halls",
                    "service_id": "smart-campus-mock",
                    "url":        "http://mock-server:8080/rest/Smart+University+Campus+API/1.0/canteen?zoneIds={{get_occupied_halls[*].zoneId | join(',', @)}}",
                    "operation":  "GET",
                    "input":      ""
                }
            ]
        }
        # WHY: when a second service needs zones from a first result, collect ALL
        # zoneIds with [*].zoneId | join(',', @) and pass them as a single zoneIds param.
        # Use zoneIds only if the endpoint description documents that parameter.

        # ── PATTERN D: two GETs → SQL for join / rank / aggregation ──────────
        ex_d = {
            "reasoning": (
                "DECOMPOSE: rank warehouses by avg temp per zone | "
                "MAP: smart-logistics-mock / GET /warehouse + GET /thermometer | "
                "CHAIN: SQL joins both on zoneId | "
                "COMBINE: sql join+aggregate over get_warehouses and get_thermometers | "
                "FILTER: no query params; avg+rank → SQL | "
                "VALIDATE: ✓ table names = task names, ✓ no {{}} in SQL"
            ),
            "tasks": [
                {
                    "task_name":  "get_warehouses",
                    "service_id": "smart-logistics-mock",
                    "url":        "http://mock-server:8080/rest/Smart+Logistics+API/1.0/warehouse",
                    "operation":  "GET",
                    "input":      ""
                },
                {
                    "task_name":  "get_thermometers",
                    "service_id": "smart-logistics-mock",
                    "url":        "http://mock-server:8080/rest/Smart+Logistics+API/1.0/thermometer",
                    "operation":  "GET",
                    "input":      ""
                },
                {
                    "task_name":  "rank_by_avg_temp",
                    "service_id": "sql-processor",
                    "url":        "",
                    "operation":  "SQL",
                    "input":      {"sql_query": "SELECT w.id, w.name, w.zoneId, AVG(t.lastReading) AS avg_temp FROM get_warehouses w JOIN get_thermometers t ON w.zoneId = t.zoneId GROUP BY w.id, w.name, w.zoneId ORDER BY avg_temp DESC"}
                }
            ]
        }
        # WHY: use SQL whenever the final answer requires COMBINING prior task results.
        # This includes: join of two datasets, aggregation (avg/sum/count), ranking,
        # grouping, set intersection, and set difference (see Example E for exclusion).
        # A plan that leaves the user with raw data from multiple GETs — when their
        # query implies a combined answer — is incomplete.
        # Reference prior task results by their task_name directly as table names.
        # Never use {{}} placeholders inside a sql_query string.

        # ── PATTERN E: two GETs → SQL set difference (exclusion language) ────
        ex_e = {
            "reasoning": (
                "DECOMPOSE: available hotels | avoid noisy districts | "
                "MAP: smart-hospitality-mock / GET /hotel + smart-acoustics-mock / GET /noise-sensor | "
                "CHAIN: SQL set difference on districtId | "
                "COMBINE: sql set_difference get_available_hotels minus get_noisy_sensors | "
                "FILTER: hotel available=true; noise-sensor level=high | "
                "VALIDATE: ✓ two GETs + terminating SQL, ✓ exclusion via NOT IN"
            ),
            "tasks": [
                {
                    "task_name":  "get_available_hotels",
                    "service_id": "smart-hospitality-mock",
                    "url":        "http://mock-server:8080/rest/Smart+Hospitality+API/1.0/hotel?available=true",
                    "operation":  "GET",
                    "input":      ""
                },
                {
                    "task_name":  "get_noisy_sensors",
                    "service_id": "smart-acoustics-mock",
                    "url":        "http://mock-server:8080/rest/Smart+Acoustics+API/1.0/noise-sensor?level=high",
                    "operation":  "GET",
                    "input":      ""
                },
                {
                    "task_name":  "hotels_in_quiet_districts",
                    "service_id": "sql-processor",
                    "url":        "",
                    "operation":  "SQL",
                    "input":      {"sql_query": "SELECT * FROM get_available_hotels WHERE districtId NOT IN (SELECT districtId FROM get_noisy_sensors)"}
                }
            ]
        }

        # WHY: when the user expresses EXCLUSION ("avoid", "without", "not near",
        # "somewhere NOT X"), the final answer is the set difference between the
        # primary list (what the user wants) and the secondary list (what to exclude).
        # Always add a terminating SQL task with NOT IN — two GETs alone return both
        # sets but leave the exclusion unresolved, so the user receives raw data
        # instead of the filtered answer they asked for.

        # ── PATTERN F: Qualitative constraint → SQL Sort (NO magic numbers) ──
        ex_f = {
            "reasoning": (
                "DECOMPOSE: find the quietest apartments | "
                "MAP: smart-real-estate-mock / GET /apartment + smart-acoustics-mock / GET /noise-sensor | "
                "CHAIN: SQL join and rank | "
                "COMBINE: sql join get_apartments and get_noise_sensors, order by noise | "
                "FILTER: user asks for 'quietest' (qualitative). I MUST NOT invent a threshold like '< 40'. I will use ORDER BY decibelLevel ASC LIMIT 3 | "
                "VALIDATE: ✓ no invented thresholds, used sorting instead"
            ),
            "tasks": [
                {
                    "task_name":  "get_apartments",
                    "service_id": "smart-real-estate-mock",
                    "url":        "http://mock-server:8080/rest/Smart+Real+Estate+API/1.0/apartment",
                    "operation":  "GET",
                    "input":      ""
                },
                {
                    "task_name":  "get_noise_sensors",
                    "service_id": "smart-acoustics-mock",
                    "url":        "http://mock-server:8080/rest/Smart+Acoustics+API/1.0/noise-sensor",
                    "operation":  "GET",
                    "input":      ""
                },
                {
                    "task_name":  "quietest_apartments",
                    "service_id": "sql-processor",
                    "url":        "",
                    "operation":  "SQL",
                    "input":      {"sql_query": "SELECT a.*, n.decibelLevel FROM get_apartments a JOIN get_noise_sensors n ON a.zoneId = n.zoneId ORDER BY n.decibelLevel ASC LIMIT 3"}
                }
            ]
        }
        # WHY: When the user asks for a qualitative extreme ("cleanest", "safest", "quietest", "cheapest")
        # DO NOT invent numeric thresholds (e.g., WHERE decibelLevel < 50). 
        # Instead, use SQL ORDER BY ... ASC or DESC with a LIMIT to find the best/worst options natively.

        examples_str = (
            f"EXAMPLE A — single GET with enum filter:\n{json.dumps(ex_a, indent=2)}\n\n"
            f"EXAMPLE B — GET list → JMESPath id extraction → PUT path param:\n{json.dumps(ex_b, indent=2)}\n\n"
            f"EXAMPLE C — GET with filter → collect zoneIds → multi-zone GET:\n{json.dumps(ex_c, indent=2)}\n\n"
            f"EXAMPLE D — two GETs → SQL join/rank/aggregate:\n{json.dumps(ex_d, indent=2)}\n\n"
            f"EXAMPLE E — two GETs → SQL set difference (exclusion):\n{json.dumps(ex_e, indent=2)}\n\n"
            f"EXAMPLE F — Qualitative constraint → SQL Sort (NO magic numbers):\n{json.dumps(ex_f, indent=2)}"
        )

        return f"""<role>
You are an API orchestrator for a distributed system.
Given a user query and a service catalog, produce ONLY a valid JSON execution plan.
</role>

<output_contract>
- Output ONLY raw JSON. Zero prose, zero markdown fences, nothing outside the JSON object.
- Top-level schema: {{"reasoning": "string", "tasks": [...]}}
- Every task must have exactly these five keys: task_name, service_id, url, operation, input.
- CONSTRAINT RULE: Before building the plan, scan the full query for constraint
  phrases ("without X", "avoiding Y", "where Z is [condition]", "near Z",
  "somewhere [adjective]"). Each constraint implies a real-time data requirement.
  Find the service in the catalog that provides that data and add a GET task for it,
  even if the user did not explicitly ask for that data.
  A plan that ignores a constraint phrase FAILS validation — add the missing task
  before marking VALIDATE as ✓.
- If the query cannot be satisfied with the available services from the catalog, output:
  {{"reasoning": "No available service can fulfil this request.", "tasks": []}}
</output_contract>


<grounding_rule> 
The catalog below is the ONLY source of truth for services, endpoints, parameters, and field names.
Treat it as a closed world: if something is not in the catalog, it does not exist.
Never invent, guess, or extrapolate service IDs, endpoint paths, parameter names, or field names.
</grounding_rule>

<reasoning_protocol>
Write the "reasoning" value BEFORE the tasks array.
Use CHAIN OF DRAFT format: one line per phase, keywords only, no full sentences.

  DECOMPOSE: <what data is needed>
  MAP:       <service-id / method endpoint> for each need
  CHAIN:     how tasks depend on each other, one of:
              - "none" (independent tasks)
              - "<target_task>.<slot> ← <source_task><jmespath>" (JMESPath chaining)
              - "SQL <op> over <source_task>[, <source_task>]" (SQL reference)
  COMBINE:   <how the final answer is produced — one of:
              "single task" /
              "chain: last task consumes task_X via JMESPath" /
              "sql filter task_X" /
              "sql join task_X and task_Y" /
              "sql set_difference task_X minus task_Y" /
              "sql set_intersection task_X and task_Y" /
              "sql aggregate/rank over task_X">
  FILTER:    <which param per GET, threshold logic if any>
  VALIDATE:  ✓ / list any issue found and how it is fixed

COMMIT RULE: write each phase once and move on.
Do not use "wait", "actually", "or perhaps", "however", "but".
If the correct interpretation is ambiguous, pick the most literal reading and commit.

COMBINE CONSISTENCY: the value you write for COMBINE must match the tasks array.
  - "single task"  → exactly one task.
  - "chain: ..."   → N HTTP tasks; the last task's url or input contains a
                     {{{{...}}}} placeholder; its raw result IS the answer (no SQL).
  - "sql ..."      → the LAST task is an SQL task.
A plan whose tasks don't match the declared COMBINE strategy FAILS validation —
fix it before writing the JSON.
</reasoning_protocol>

<hard_constraints>
These rules are absolute and may never be violated.

HC-1  CLOSED WORLD
      Use only services, endpoints, parameters, and field names present in the catalog.

HC-2  NO UNRESOLVED PLACEHOLDERS
      Every url must be fully resolved. {{id}}, {{zoneId}} and similar bare placeholders
      are forbidden. Use chaining expressions {{{{task<expr>}}}} or literal values only.

HC-3  REQUIRED KEYS
      Every task must have: task_name, service_id, url, operation, input.
      service_id must be the exact SERVICE_ID string from the catalog (not the name).

HC-4  REQUIRED PARAMETERS
      Parameters marked * in the catalog are required and must appear in every url.

HC-5  OPERATION VALUES
      operation must be exactly one of: GET  POST  PUT  DELETE  SQL

HC-6  URL RULES
      - Non-SQL tasks: url must start with http:// and be non-empty.
      - SQL tasks: url must be an empty string "".

HC-7  NO DUPLICATE CALLS
      Never build a task that calls the same url+service as an earlier task.
      Reuse earlier results via JMESPath or a SQL task instead.

HC-8  NO CONCATENATED PLACEHOLDERS
      Never write ?param={{{{task1[*].f | join(',',@)}}}},{{{{task2[*].f | join(',',@)}}}}
      Collect all needed data in one prior task and filter with a JMESPath OR expression.

HC-9  PARAMETER VALUES FROM CATALOG
      When setting a query parameter value, copy it verbatim from the catalog's parameter
      examples or enum list — never from the user's query text. The user may use different
      casing, abbreviations, or synonyms. The catalog value is always authoritative.
      Example: if the catalog shows categoryId example "NARRATIVE" and the user writes
      "narrative" or "Narrative", use "NARRATIVE".

HC-10  NO INVENTED THRESHOLDS
    Never invent numeric thresholds in WHERE clauses (e.g. "< 50", "> 100") UNLESS the user explicitly specifies an exact number in their query.
    If the user asks for qualitative states (e.g. "clean x", "quiet x", "cheap x") without providing numbers:
    - To find extremes, use an SQL task with ORDER BY field ASC/DESC LIMIT N.
    - To filter by "good/bad/safe" conditions, use catalog-documented boolean or enum fields (e.g. alertActive=false, status='ok').

HC-11  JMESPATH IS URL/INPUT SUBSTITUTION ONLY
      JMESPath placeholders {{{{task<expr>}}}} serve EXACTLY ONE purpose: injecting
      values from a prior task's result into the url or input of a subsequent
      HTTP task. This is the CHAIN mechanism — valid and expected across any
      number of chained HTTP tasks (see Examples B, C).

      JMESPath is NOT a result-combination mechanism. Use an SQL terminal task
      when the final answer requires ANY of the following over prior results:
        - merging rows from TWO OR MORE task results (join, set intersect/diff)
        - aggregation (sum, avg, count, min, max, group by)
        - ranking or sorting across a dataset (order by + limit)
        - post-filtering a single task when the HTTP API could not filter it
          server-side

      Therefore: min_by, max_by, sort_by MUST NOT appear inside {{{{...}}}} —
      express them as SQL (ORDER BY ... LIMIT, MIN, MAX, GROUP BY) in a
      terminal SQL task.

      Quick decision table:
        one GET, API does it all                    → COMBINE "single task",   no SQL
        GET → GET/POST/PUT/DELETE via JMESPath url  → COMBINE "chain: ...",    no SQL
        two+ GETs whose results must be merged      → COMBINE "sql ...",       last task SQL
        one GET + post-aggregation/ranking          → COMBINE "sql ...",       last task SQL@@HC12_BLOCK@@
</hard_constraints>

<soft_constraints>
SC-1  MULTI-ZONE QUERIES
      When an endpoint's description documents a ?zoneIds= parameter for multi-zone queries,
      pass zones from a prior task as: ?zoneIds={{{{prev[*].zoneId | join(',', @)}}}}

SC-2  PATH PARAMETER INJECTION
      Inject ids and keys directly into the url string using chaining syntax.
      Never put them in the input field.
</soft_constraints>

<self_check>
Before writing the final JSON, verify every item below:

  □ Every service_id is copied verbatim from the catalog's SERVICE_ID field.
  □ Every endpoint path is copied verbatim from the catalog.
  □ Every url is copied verbatim from the catalog's URL field — not from memory.
  □ Every parameter name is copied verbatim from the catalog (no guessing synonyms).
  □ No url contains bare {{...}} unless it is a valid chaining expression {{{{task<expr>}}}}.
@@HC12_SELFCHECK@@
  □ All required (*) parameters are present in every url.
  □ No two tasks call the same url+service.
  □ SQL tasks have url="" and input={{"sql_query":"..."}}.
  □ Non-SQL tasks have a non-empty url starting with http://.
  □ COMBINE phase matches tasks:
      "single task" → exactly 1 task.
      "chain: ..."  → N HTTP tasks; last task's url or input contains {{...}}; no SQL.
      "sql ..."     → last task is SQL.
  □ If any catalog lookup failed in PHASE 2, tasks is an empty array [].
</self_check>

<jmespath_reference>
JMESPath serves ONE purpose: inject values from a prior task's result into the
url or input of a subsequent HTTP task. Syntax: {{{{task_name<expr>}}}}.

Most API responses wrap their array under a named key (e.g. "items", "results").
You MUST include that key between the task name and the filter/index.
WRONG: {{{{get_playlists[?name=='My Rock'] | [0].id}}}}  ← filter on a dict, always None
RIGHT: {{{{get_playlists.items[?name=='My Rock'] | [0].id}}}}  ← filter on the array

Exception: if the response IS directly a list at the root, omit the wrapper key.

Four canonical patterns — nothing else is permitted inside {{{{...}}}}:

  1. Filter + pick first:    {{{{t.array_key[?k=='v'] | [0].field}}}}   ← pipe required
                             Example: {{{{get_playlists.items[?name=='My Rock'] | [0].id}}}}
  2. All values (array):     {{{{t.array_key[*].field}}}}
  3. Joined string:          {{{{t.array_key[*].field | join(',', @)}}}} ← string fields only
  4. Positional first item:  {{{{t.array_key[0].field}}}}
                             DO NOT write {{{{t.array_key}}}} alone — that injects the
                             whole array into the URL and breaks routing.

Pattern 1 is combinable with any filter expression; pattern 3 accepts an
optional filter before the pipe: {{{{t.array_key[?k=='v'].field | join(',', @)}}}}.

Filter expressions inside [?...]:
  operators  ==  !=  <  >  <=  >=  &&  ||
  functions  contains(field, 'text')
  literals   'strings'   `numbers`   `true`   `false`   `null`

Examples of valid filters (plug into pattern 1 or 3):
  [?status=='open']                 [?alertActive==`true`]
  [?price<`100`]                    [?contains(name, 'Rossi')]
  [?a=='x' || a=='y']               [?field==`null`]

CRITICAL RULES:
  - Always include the array wrapper key: t.items[?...] not t[?...].
  - join(',', @) works ONLY on string fields. Never on integers or arrays.
  - [?k=='v'] | [0].field — the pipe is mandatory to extract a single value.
  - Ranking, sorting, aggregation, grouping, and combining two prior tasks
    are ALWAYS SQL — never JMESPath. min_by, max_by, sort_by MUST NOT
    appear inside {{{{...}}}}. Use SQL's ORDER BY, LIMIT, MIN, MAX, GROUP BY.
</jmespath_reference>

<examples>
Study the WHY comment after each example. It states the abstract principle.
Apply the principle to any domain — do not imitate the specific services or field names.

{examples_str}
</examples>

<sql_reference>
SQL tasks use DuckDB dialect. Reference prior task results by task_name as table name.
Never use {{{{}}}} placeholders inside the sql_query string.
NEVER use SQL reserved words as task_name (e.g. order, group, select, index, table, user).

Common patterns:
  Post-filter:    SELECT * FROM t WHERE field = 'value'
                  SELECT col1, col2 FROM t WHERE cond1 AND cond2
  Sort + top-N:   SELECT * FROM t ORDER BY field ASC LIMIT 1
                  SELECT * FROM t ORDER BY field DESC LIMIT 1
                  SELECT * FROM t ORDER BY field ASC LIMIT N
  Aggregate:      SELECT MIN(field), MAX(field), AVG(field) FROM t
  Join:           SELECT a.*, b.field FROM task_a a JOIN task_b b ON a.zoneId = b.zoneId
  Intersect:      SELECT * FROM task_a WHERE zoneId IN (SELECT zoneId FROM task_b WHERE cond)
  Difference:     SELECT * FROM task_a WHERE zoneId NOT IN (SELECT zoneId FROM task_b)
</sql_reference>"""

    def _build_user_prompt(self, discovered_services, discovered_capabilities,
                           discovered_endpoints, discovered_schemas,
                           discovered_request_schemas, discovered_parameters,
                           query, input_files=None) -> str:
        """Builds the user prompt with all the details of the discovered services.

        The user prompt includes:
        - the list of services and their endpoints,
        - parameter descriptions and request/response schemas,
        - any attached files,
        - the user query.
        """
        lines = ["SERVICES AND ENDPOINTS:"]
        for i, service in enumerate(discovered_services):
            lines.append(f"\nSERVICE_ID: {service.get('_id')}")
            lines.append(f"NAME: {service.get('name')}")
            caps   = discovered_capabilities[i]   if i < len(discovered_capabilities)   else {}
            eps    = discovered_endpoints[i]       if i < len(discovered_endpoints)       else {}
            rsch   = discovered_schemas[i]         if i < len(discovered_schemas)         else {}
            qsch   = discovered_request_schemas[i] if i < len(discovered_request_schemas) else {}
            params = discovered_parameters[i]      if i < len(discovered_parameters)      else {}
            for key in caps:
                if key == "POST /register":
                    continue
                lines.append(f"  {key}")
                lines.append(f"    URL: {eps.get(key, 'N/A')}")
                lines.append(f"    DESC: {caps[key]}")
                if params.get(key):
                    lines.append(f"    PARAMETERS (* = required): {params[key]}")
                if rsch.get(key):
                    lines.append(f"    RESPONSE SCHEMA: {rsch[key]}")
                if qsch.get(key):
                    lines.append(f"    REQUEST SCHEMA (* = required): {qsch[key]}")

        return f"""{chr(10).join(lines)}

FILES:
{str(input_files) if input_files else "none"}

QUERY:
{query}"""

if __name__ == "__main__":
    # Export the template (the ONLY one, with sentinels) as an editable base:
    #   python -m service.planning.prompt_provider config/prompts/planner_system.md
    out = sys.argv[1] if len(sys.argv) > 1 else "planner_system.md"
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(PromptProvider()._build_template(), encoding="utf-8")
    print("exported", out)