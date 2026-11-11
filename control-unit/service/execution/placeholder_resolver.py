import json
import re
import jmespath
from jmespath import exceptions as jmespath_exc


class PlaceholderResolver:
    """JMESPath {{...}} chaining resolution over the context of previous tasks.
    Moved 1:1 from the old Controller (methods _resolve_expression and
    resolve_placeholders): no logic changes."""

    def _resolve_expression(self, expr: str, context: dict):
        """
        Resolves a JMESPath placeholder against the context of previous tasks.

        Format:  task_name<jmespath_expression>

        Canonical patterns (see HC-11 in the system prompt — the ONLY allowed ones):
          get_bins[0].id                                         # single value
          get_attractions[?status=='open'] | [0].zoneId          # filter + pick
          get_sensors[*].zoneId                                  # array
          get_sensors[?alertActive==`true`].zoneId | join(',',@) # filtered join

        Legacy patterns supported for graceful degradation (but FORBIDDEN by the prompt —
        if the model generates them anyway, the code does not crash but it is a bug):
          get_lights | sort_by(@, &brightness)[0].id
          min_by(get_sensors, &reading)
        For ranking/min/max use a terminal SQL task.
        """
        # Remove legacy suffixes
        expr = re.sub(r'\.(output|response|data)\b', '', expr)
        # Normalize .[N] → [N]: valid JMESPath indexing is 'results[0]',
        # NOT 'results.[0]' (a dot before the bracket is a parse error).
        # Remove any spurious dot, leaving '| [0]' (pipe) intact.
        expr = re.sub(r'\.(\[\d+\])', r'\1', expr)
        m = re.match(r'^(\w+)(.*)', expr, re.DOTALL)
        if not m:
            print(f"[JMESPATH] Unable to extract task_name from '{expr}'")
            return ""

        task_name = m.group(1)
        remainder = m.group(2).strip()

        # ── JMESPath functions used as a prefix (min_by, max_by, sort_by, etc.) ──
        # The regex captures "min_by" as task_name, but it is not a task in the context:
        # it is a JMESPath function. In this case we evaluate the whole expression
        # on the context dict, where task_names are keys resolvable by JMESPath.
        JMESPATH_FUNCTIONS = {"min_by", "max_by", "sort_by", "length", "keys",
                              "values", "contains", "starts_with", "ends_with",
                              "reverse", "to_array", "to_string", "to_number", "type"}
        if task_name in JMESPATH_FUNCTIONS:
            try:
                result = jmespath.search(expr, context)
                if result is None or result == [] or result == "":
                    print(f"[JMESPATH] Function '{task_name}' — no result for '{expr}'")
                    return ""
                return result
            except jmespath_exc.JMESPathError as e:
                print(f"[JMESPATH ERROR] function '{task_name}': {e}")
                return ""

        val = context.get(task_name)
        if val is None:
            print(f"[JMESPATH] Task '{task_name}' not found in the context")
            return ""

        if not remainder:
            return val

        # ── Leading pipe (e.g. task | sort_by(@, &field)[0].id) ─────────────────
        # The regex extracts "task" as task_name and "| sort_by(...)" as remainder.
        # The pipe is a binary operator: it cannot start a JMESPath expression.
        # Fix: strip the leading | — val is already the left-hand side of the pipe.
        if remainder.startswith('|'):
            remainder = remainder[1:].strip()

        # Remove the leading dot if present (JMESPath does not accept it)
        jmespath_expr = remainder[1:] if remainder.startswith('.') else remainder

        if not jmespath_expr:
            return val

        try:
            result = jmespath.search(jmespath_expr, val)

            # ── Autofix: [?] filter applied to the root dict instead of the array ────
            # Typical cause: the LLM writes task[?k=='v'] | [0].f but the response is
            # {"items": [{k,v}, ...]} — the filter must go on .items, not on the root.
            # If result is None and the expr starts with [?, try the most common
            # wrapper keys, but only if actually present in the response dict.
            if result is None and jmespath_expr.lstrip().startswith('[?') and isinstance(val, dict):
                _WRAPPERS = ('items', 'results', 'data', 'tracks', 'albums',
                             'artists', 'playlists', 'devices', 'queued_tracks')
                for _w in _WRAPPERS:
                    if _w not in val:
                        continue
                    _candidate = jmespath.search(f'{_w}{jmespath_expr}', val)
                    if _candidate is not None and _candidate != [] and _candidate != "":
                        print(
                            f"[JMESPATH AUTOFIX] '{task_name}{remainder}': filter applied "
                            f"to '.{_w}' (auto-detected wrapper). "
                            f"Write '{task_name}.{_w}{jmespath_expr}' to be explicit."
                        )
                        result = _candidate
                        break

            if result is None:
                print(f"[JMESPATH] No match for '{jmespath_expr}' on '{task_name}'")
                return ""
            # Empty list after filtering → the resulting param will be empty
            # (e.g. zoneIds= ), which causes a 400 on Microcks — log an explicit warning
            if isinstance(result, list) and len(result) == 0:
                print(f"[JMESPATH WARN] Empty list for '{jmespath_expr}' on '{task_name}' — resulting URL param will be empty")
                return ""
            # Deduplication: lists of strings (e.g. zoneIds for join)
            if isinstance(result, list) and all(isinstance(x, str) for x in result):
                seen = set()
                result = [x for x in result if not (x in seen or seen.add(x))]
            return result
        except jmespath_exc.JMESPathError as e:
            print(f"[JMESPATH ERROR] expr='{expr}' jmespath='{jmespath_expr}': {e}")
            # Do NOT return the parent (dict/list): it would be serialized with
            # json.dumps inside the URL, producing a silent 404. Fail
            # visibly by returning an empty string.
            return ""

    def resolve_placeholders(self, data, context: dict):
        """
        Resolves all {{...}} placeholders in a data structure.
        Supports strings, dictionaries and lists recursively.
        """
        if isinstance(data, str):
            # ── Normalize malformed concatenation generated by the LLM ──────────
            # Wrong pattern: {{A},{{B}}  (missing a } before the comma)
            # Expected pattern: {{A}},{{B}} (two distinct placeholders)
            # Cause: the model closes the first with } instead of }} to concatenate.
            # Scoped fix: look only for the sequence "},{{" that is NOT already preceded by "}"
            # (i.e. not already corrected as "}},{{"), avoiding false replacements on
            # legitimate "},{" characters outside placeholders.
            fixed = re.sub(r'(?<!\})\},(\{\{)', r'}},\1', data)
            if fixed != data:
                print(f"[PLACEHOLDER FIX] Malformed concatenation normalized in: {data[:80]}")
                data = fixed

            matches = re.findall(r'\{\{(.*?)\}\}', data)
            if not matches:
                return data

            # Case 1: the whole string is exactly one placeholder → preserve the type
            if len(matches) == 1 and data.strip() == f"{{{{{matches[0]}}}}}":
                return self._resolve_expression(matches[0].strip(), context)

            # Case 2: inline placeholder in URL or string → convert everything to string
            for match in matches:
                val = self._resolve_expression(match.strip(), context)
                if isinstance(val, (dict, list)):
                    val = json.dumps(val)
                elif val is None:
                    val = ""
                else:
                    val = str(val)
                data = data.replace(f"{{{{{match}}}}}", val)
            return data

        if isinstance(data, dict):
            return {k: self.resolve_placeholders(v, context) for k, v in data.items()}
        if isinstance(data, list):
            return [self.resolve_placeholders(item, context) for item in data]
        return data
