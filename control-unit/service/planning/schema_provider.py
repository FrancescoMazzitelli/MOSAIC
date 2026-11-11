"""Provider of the schemas used by the planner for LLM guided decoding.

The provider supplies:
- the output schema for plan generation (`output_schema()`),
- the dynamic schema of the `input` field based on the request_schemas discovered
  in the registry (`input_schema()`).

The output schema can be overridden by an external JSON file via
the `PLANNER_OUTPUT_SCHEMA_PATH` environment variable. When no override is set,
a default schema defined internally is used.
"""

import json
import os
import re
from pathlib import Path

_DEFAULT_SCHEMA = (Path(__file__).resolve().parents[2]
                   / "config" / "schemas" / "plan_output.json")


class SchemaProvider:
    """Provides the schemas used by the planner to format the output.

    `output_schema()` returns the JSON format schema that guides the
    plan generation by the LLM.

    `input_schema()` builds a data-driven schema for the `input` field
    by aggregating all the request_schemas available in the registry.
    """

    def __init__(self):
        self.output_schema_path = os.environ.get("PLANNER_OUTPUT_SCHEMA_PATH")

    def output_schema(self) -> dict:
        """Returns the JSON output schema used by the planner.

        If the `PLANNER_OUTPUT_SCHEMA_PATH` environment variable points to a valid
        JSON file, that one is used. Otherwise the default schema
        defined internally is returned.
        """
        path = self.output_schema_path or (_DEFAULT_SCHEMA if _DEFAULT_SCHEMA.exists() else None)
        if path and Path(path).exists():
            return json.loads(Path(path).read_text(encoding="utf-8"))
        return self._default_output_schema()

    def _default_output_schema(self) -> dict:
        """Default schema for the planner output.

        This schema describes an object with:
        - `reasoning`: the planner's reasoning,
        - `tasks`: list of tasks with name, service, URL, operation and input.
        """
        return {
            "type": "object",
            "properties": {
                "reasoning": {"type": "string"},
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "task_name":  {"type": "string"},
                            "service_id": {"type": "string"},
                            "url":        {"type": "string"},
                            "operation":  {"type": "string",
                                           "enum": ["GET", "POST", "PUT", "DELETE", "SQL"]},
                            "input":      {"type": ["string", "object", "null"]},
                        },
                        "required": ["task_name", "service_id", "url", "operation", "input"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["tasks"],
            "additionalProperties": False,
        }

    # --- input_schema: moved 1:1 from Controller._build_input_format_schema ---
    def input_schema(self, discovered_request_schemas: list) -> dict:
        """Builds the JSON schema for the `input` field of the tasks.

        Aggregates all valid fields from the request_schemas discovered in the registry and
        produces a schema with `additionalProperties: false`.

        This schema is injected into Ollama's `format` for guided
        decoding: this way the LLM cannot generate keys not provided by the
        registered services.

        Args:
            discovered_request_schemas (list): list of `endpoint -> schema` dictionaries
                discovered in the registry.

        Returns:
            dict: valid JSON schema for the `input` field.
        """
        field_pattern = re.compile(r'(\w+):([\w]+)(?:\([^)]*\))?\*?')

        # Map compact type → JSON schema with string as alternative for JMESPath
        type_map = {
            "arr":   {"type": ["array",   "string"]},
            "str":   {"type": "string"},
            "int":   {"type": ["integer", "string"]},
            "float": {"type": ["number",  "string"]},
            "bool":  {"type": ["boolean", "string"]},
            "obj":   {"type": ["object",  "string"]},
            "enum":  {"type": "string"},
            "any":   {},
        }

        all_properties: dict = {}

        for schemas_per_service in discovered_request_schemas:
            if not isinstance(schemas_per_service, dict):
                continue
            for endpoint, schema_str in schemas_per_service.items():
                if not schema_str:
                    continue
                for match in field_pattern.finditer(schema_str):
                    field_name = match.group(1)
                    field_type = match.group(2)
                    if field_name not in all_properties:
                        all_properties[field_name] = type_map.get(field_type, {})

        if not all_properties:
            # No request_schema in the registry → permissive fallback
            print("[INPUT SCHEMA] No request_schema found, using permissive fallback.")
            return {"type": ["string", "object", "null"]}

        # Add sql_query as a global key for SQL tasks
        # Without this, guided decoding blocks the SQL string (type object ≠ string)
        all_properties["sql_query"] = {"type": "string"}

        print(f"[INPUT SCHEMA] Allowed keys for 'input': {sorted(all_properties.keys())}")
        return {
            "type":                 "object",
            "properties":          all_properties,
            "additionalProperties": False,   # ← hard block on hallucinations
        }
