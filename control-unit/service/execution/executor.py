"""Control Unit Executor.

This module executes a JSON execution plan produced by the planner.
It supports:
- placeholder chaining resolution via `PlaceholderResolver`,
- asynchronous dispatch of HTTP calls to remote services,
- in-process execution of SQL tasks with DuckDB,
- collection of the result context between tasks.

The `Executor` has no dependency on the LLM: it receives an already built plan and
only executes it and returns its results.
"""

import os
import re
import math
import json
import asyncio
import mimetypes
import aiohttp
import duckdb
import pandas as pd
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from service.execution.placeholder_resolver import PlaceholderResolver


class Executor:
    """Executes a JSON execution plan.

    The `Executor` handles the real requests to the services and the internal
    SQL computation. The chaining between tasks is resolved via `PlaceholderResolver`.

    The class is designed to keep the execution logic separate from the
    planning logic and the LLM rules.
    """

    def __init__(self, resolver: PlaceholderResolver = None):
        self.resolver = resolver or PlaceholderResolver()
        self.backend_mode = "MOCK"

    def run(self, plan: dict, discovered_services, backend_mode: str = "MOCK"):
        """Executes the plan synchronously.

        Args:
            plan (dict): JSON plan with the `tasks` key.
            discovered_services (list): discovered services used by the task execution.
            backend_mode (str): execution mode, typically 'MOCK' or 'REAL'.

        Returns:
            list | dict: results of the execution pipeline, or the result
                of a file to download if a task returns status FILE.
        """
        self.backend_mode = backend_mode
        return asyncio.run(self.trigger_agents_async(plan, discovered_services))

    # -- methods moved from the old Controller -----------------------------
    def _build_auth_headers(self) -> dict:
        """
        Builds the authentication headers for HTTP calls.

        Source: API_AUTH_HEADERS environment variable containing a JSON,
        e.g.:
            API_AUTH_HEADERS='{"Authorization": "Bearer xyz", "X-Api-Key": "abc"}'

        The JSON format covers all common cases without hardcoding assumptions:
          - simple API key   → {"X-Api-Key": "..."}
          - Bearer token     → {"Authorization": "Bearer ..."}
          - custom header    → {"X-Custom": "..."}
          - multiple headers together (e.g. auth + tracing) on the same env var.

        Returns {} if the variable is not set or is not valid JSON,
        the typical case in MOCK mode (Microcks does not require auth by default).

        Future extension: per-service via API_AUTH_HEADERS__<service_id>.
        The injection point (call_agent) already goes through this method, it is enough
        to add the conditional read here without touching the caller.
        """
        raw = os.environ.get("API_AUTH_HEADERS", "")
        if not raw:
            return {}
        try:
            headers = json.loads(raw)
            if isinstance(headers, dict):
                return {str(k): str(v) for k, v in headers.items()}
            print(f"[AUTH] API_AUTH_HEADERS is not a JSON object, ignored")
        except json.JSONDecodeError as e:
            print(f"[AUTH] API_AUTH_HEADERS is not valid JSON ({e}), ignored")
        return {}

    async def call_agent(self, session, task, discovered_services):
        """Sends a single HTTP request for a non-SQL task.

        This method supports GET, POST, PUT, PATCH and DELETE. It handles:
        - resolution of relative endpoints in MOCK mode,
        - query string normalization,
        - authentication via API_AUTH_HEADERS,
        - multipart form-data for custom FILE/TEXT tags in the payload.

        Args:
            session (aiohttp.ClientSession): asynchronous HTTP session.
            task (dict): description of the task to execute.
            discovered_services (list): discovered services, not used directly here but
                kept for compatibility with the past behavior.

        Returns:
            dict: result with keys `task_name`, `operation`, `url_template`,
                `url_resolved`, `status`, `status_code`, `result`.
        """
        task_name  = task.get("task_name") or "unnamed_task"
        endpoint   = task.get("endpoint") or task.get("url") or ""
        input_data = task.get("input", "")
        operation  = str(task.get("operation") or "GET").upper()

        # Strip any HTTP method prefix ("GET /path" → "/path").
        # NB: do NOT use endpoint.split(" ")[-1] — it would break URLs with spaces in the
        # query string (e.g. ?query=The Dark Knight → "Knight"), causing 404.
        _verb = re.match(r'^\s*(?:GET|POST|PUT|DELETE|PATCH)\s+(\S.*)$', endpoint, re.IGNORECASE)
        if _verb:
            endpoint = _verb.group(1).strip()
        # Prepend MOCK_SERVER_URL only in MOCK mode. In REAL a non-http URL
        # is a planning bug: let the request fail naturally.
        if self.backend_mode == "MOCK" and endpoint and not endpoint.startswith("http"):
            mock_url = os.environ.get("MOCK_SERVER_URL", "http://mock-server:8080")
            endpoint = f"{mock_url}{endpoint}" if endpoint.startswith("/") else f"{mock_url}/{endpoint}"
        # Defensive query string re-encoding: makes the URL robust against spaces and
        # non-encoded characters emitted by the planner (e.g. ?query=The Dark Knight).
        if endpoint.startswith("http") and "?" in endpoint:
            _p = urlsplit(endpoint)
            endpoint = urlunsplit(
                _p._replace(query=urlencode(parse_qsl(_p.query, keep_blank_values=True)))
            )

        response_result = {
            "task_name": task_name,
            "operation": operation,
            # URL planned by the planner (may contain {{...}})
            "url_template": task.get("url", ""),
            # URL actually used in the HTTP call (placeholders already resolved)
            "url_resolved": endpoint,
        }

        if not endpoint or not endpoint.strip():
            print(f"[WARN] Ghost task '{task_name}' ignored.")
            response_result.update({"status": "SUCCESS", "status_code": 200, "result": {}})
            return response_result

        # Authentication headers (empty if not configured, e.g. MOCK)
        auth_headers = self._build_auth_headers()

        try:
            tag_pattern = r"\[(\w+)\](.*?)\[/\1\]"
            tag_matches = re.findall(tag_pattern, str(input_data), re.DOTALL) \
                          if isinstance(input_data, str) else []

            if operation == "GET":
                async with session.get(endpoint, headers=auth_headers) as resp:
                    status = resp.status
                    try:
                        result = await resp.json()
                    except Exception:
                        result = await resp.text()
                    response_result.update({
                        "status":      "SUCCESS" if status in (200, 201, 204) else "ERROR",
                        "status_code": status,
                        "result":      result,
                    })

            elif operation in ("POST", "PUT", "PATCH"):
                if tag_matches:
                    form_data  = aiohttp.FormData()
                    open_files = []   # track opened files to close them after the request
                    for tag_type, tag_content in tag_matches:
                        tag_content = tag_content.strip()
                        if tag_type == "FILE":
                            file_path = os.path.join("Files", tag_content)
                            if os.path.exists(file_path):
                                file_obj = open(file_path, "rb")
                                open_files.append(file_obj)
                                form_data.add_field(
                                    "file", file_obj,
                                    filename=tag_content,
                                    content_type=mimetypes.guess_type(file_path)[0]
                                                 or "application/octet-stream"
                                )
                        elif tag_type == "TEXT":
                            form_data.add_field("data", tag_content, content_type="application/json")
                    try:
                        async with session.request(operation, endpoint, data=form_data, headers=auth_headers) as resp:
                            status = resp.status
                            try:
                                result = await resp.json()
                            except Exception:
                                result = await resp.text()
                            response_result.update({
                                "status": "SUCCESS" if status in (200, 201, 204) else "ERROR",
                                "status_code": status, "result": result,
                            })
                    finally:
                        for f in open_files:
                            f.close()
                else:
                    payload = input_data if isinstance(input_data, dict) else {}
                    async with session.request(operation, endpoint, json=payload, headers=auth_headers) as resp:
                        status = resp.status
                        try:
                            result = await resp.json()
                        except Exception:
                            result = await resp.text()
                        response_result.update({
                            "status": "SUCCESS" if status in (200, 201, 204) else "ERROR",
                            "status_code": status, "result": result,
                        })

            elif operation == "DELETE":
                async with session.delete(endpoint, headers=auth_headers) as resp:
                    status = resp.status
                    try:
                        result = await resp.json()
                    except Exception:
                        result = await resp.text()
                    response_result.update({
                        "status": "SUCCESS" if status in (200, 201, 204) else "ERROR",
                        "status_code": status, "result": result,
                    })

        except Exception as e:
            print(f"[EXCEPTION] '{task_name}': {e}")
            response_result.update({"status": "EXCEPTION", "status_code": 500, "result": str(e)})

        return response_result

    def _execute_sql_task(self, task: dict, context: dict) -> dict:
        """
        Executes a SQL task (operation: SQL) using in-process DuckDB.

        Each entry of the execution_context is registered as a DuckDB table
        with the name of the task that produced it. The SQL query in the 'input' field
        can reference any previous task directly by name,
        without needing JMESPath {{...}} placeholders.

        Returns a dict compatible with the call_agent result format.
        """
        task_name  = task.get("task_name") or "sql_task"
        input_data = task.get("input", {})

        # input is an object {"sql_query": "SELECT ..."} — extract the query
        if isinstance(input_data, dict):
            sql_query = input_data.get("sql_query", "")
        else:
            sql_query = ""

        if not sql_query:
            return {
                "task_name":   task_name,
                "operation":   "SQL",
                "status":      "ERROR",
                "status_code": 400,
                "result":      "SQL task requires input={'sql_query': 'SELECT ...'}",
            }

        tail = "..." if len(sql_query) > 120 else ""
        print(f"[SQL] Task '{task_name}': {sql_query[:120]}{tail}")

        # Do not rewrite the SQL query with regex: it can corrupt valid tokens
        # (e.g. ORDER BY). If task_name is a reserved keyword, the query
        # will fail and be handled by the except block.

        try:
            conn = duckdb.connect()   # in-memory database, isolated per task
        except Exception as e:
            return {
                "task_name":   task_name,
                "operation":   "SQL",
                "status":      "ERROR",
                "status_code": 500,
                "result":      f"DuckDB connect error: {e}",
            }

        try:
            # Register each previous result as a DuckDB table
            for tbl, data in context.items():
                escaped_tbl = str(tbl).replace('"', '""')

                if isinstance(data, list):
                    if not data:
                        # Empty list: register a sentinel table — unknown columns
                        # produce a Binder Error if the query references them,
                        # handled in the except block as a graceful empty result
                        conn.execute(f'CREATE TABLE "{escaped_tbl}" (dummy VARCHAR)')
                    else:
                        df = pd.DataFrame(data)
                        conn.register(str(tbl), df)
                elif isinstance(data, dict):
                    df = pd.DataFrame([data])
                    conn.register(str(tbl), df)

            rel  = conn.execute(sql_query)
            rows = rel.fetchall()
            cols = [desc[0] for desc in rel.description]

           
            result = []
            for row in rows:
                row_dict = {}
                for col, val in zip(cols, row):
                    if isinstance(val, float) and math.isnan(val):
                        val = None                  # NaN → null JSON
                    elif hasattr(val, 'isoformat'):
                        val = val.isoformat()       # datetime → ISO string
                    row_dict[col] = val
                result.append(row_dict)

            print(f"[SQL] Task '{task_name}' completed — {len(result)} rows.")
            return {
                "task_name":   task_name,
                "operation":   "SQL",
                "status":      "SUCCESS",
                "status_code": 200,
                "result":      result,
            }

        except Exception as e:
            error_msg = str(e)
            print(f"[SQL ERROR] Task '{task_name}': {error_msg}")

            
            if "Binder Error" in error_msg or "dummy" in error_msg:
                print(f"[SQL WARN] Task '{task_name}': Binder Error on empty table — returned empty result")
                return {
                    "task_name":   task_name,
                    "operation":   "SQL",
                    "status":      "SUCCESS",
                    "status_code": 200,
                    "result":      [],
                }

            return {
                "task_name":   task_name,
                "operation":   "SQL",
                "status":      "ERROR",
                "status_code": 500,
                "result":      error_msg,
            }

        finally:
            conn.close()   # guarantees closure even on exception

    async def trigger_agents_async(self, agents: dict, discovered_services):
        """Executes the plan tasks asynchronously.

        For each task in `agents['tasks']`:
        - resolves JMESPath placeholders via `PlaceholderResolver`;
        - executes HTTP tasks with `call_agent()`;
        - executes SQL tasks with `_execute_sql_task()`;
        - updates the `context` with the results of successful tasks.

        If a task returns a `FILE` status, the pipeline stops
        immediately and returns that result.

        Args:
            agents (dict): execution plan with the `tasks` key.
            discovered_services (list): discovered services used for compatibility.

        Returns:
            list | dict: results of the executed tasks or the FILE result.
        """
        results = []
        context = {}
        async with aiohttp.ClientSession() as session:
            for task in agents.get("tasks", []):
                operation = str(task.get("operation") or "GET").upper()

                # ── SQL task: in-process DuckDB execution ─────────────────────
                if operation == "SQL":
                    result = self._execute_sql_task(task, context)
                else:
                    # Keep the LLM template in task['url'] for the client,
                    # use a separate endpoint for the actual call.
                    task["endpoint"] = self.resolver.resolve_placeholders(task.get("url") or "", context)
                    task["url_resolved"] = task["endpoint"]
                    if task.get("input"):
                        task["input"] = self.resolver.resolve_placeholders(task["input"], context)
                    result = await self.call_agent(session, task, discovered_services)

                if result.get("status") == "FILE":
                    return result
                results.append(result)
                name = task.get("task_name") or "unnamed_task"
                if result.get("status") == "SUCCESS":
                    raw = result.get("result", {})
                    if isinstance(raw, (dict, list)):
                        context[name] = raw
                    else:
                        print(f"[CONTEXT] '{name}' returned non-JSON ({type(raw).__name__}) — not stored in chain context")
                        context[name] = {}
                else:
                    print(f"[CHAIN BROKEN] '{name}' failed. Pipeline interrupted.")
                    break
        return results
