"""Client for calling Ollama with guided decoding.

This module encapsulates the request to Ollama's `/api/chat` endpoint.
The payload includes the model to use, the required output format and the
system/user messages. The `complete()` method dynamically injects the
input schema into the provided output format, so as to validate the
structure of the generated responses.
"""

import os
import time
import copy
import requests


class LLMClient:
    """Isolates the call to Ollama (/api/chat with guided decoding).

    The output schema comes FROM OUTSIDE (SchemaProvider) and is completed here
    by injecting the dynamic schema of the 'input' field built from the registry's
    request_schemas. It is equivalent to the old query_ollama, but format and input are no
    longer hardcoded in the method.
    """

    def __init__(self, model_name: str):
        self.model_name = model_name
        self.url = os.environ.get("OLLAMA_API_URL", "http://localhost:11434")

    def complete(self, system_prompt: str, user_prompt: str,
                 output_schema: dict, input_schema: dict | None = None) -> tuple[str, float]:
        """Asks Ollama to complete a prompt with guided decoding, dynamically injecting
        the input schema inside the output schema.

        Args:
            system_prompt (str): system prompt for the model.
            user_prompt (str): user prompt to send to the model.
            output_schema (dict): JSON output schema required by Ollama.
            input_schema (dict | None): schema of the `input` field to inject into the
                output format, if available.

        Returns:
            tuple[str, float]: pair containing the generated text and the processing
                time in seconds.

        Raises:
            RuntimeError: on HTTP error or response parsing error.
        """
        schema = copy.deepcopy(output_schema)
        try:
            # Add the input schema inside the tasks before
            # sending the request to Ollama, preserving the external output_schema.
            schema["properties"]["tasks"]["items"]["properties"]["input"] = \
                input_schema or {"type": ["string", "object", "null"]}
        except (KeyError, TypeError):
            # If the schema has a different structure than expected, use it
            # without modifications.
            pass

        try:
            t0 = time.perf_counter()
            response = requests.post(
                f"{self.url}/api/chat",
                json={
                    "model": self.model_name,
                    "format": schema,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": user_prompt},
                    ],
                    "think": False,
                    "options": {"temperature": 0.0, "num_ctx": 16384},
                    "stream": False,
                },
                timeout=120,
            )
            response.raise_for_status()
            return response.json()["message"]["content"].strip(), time.perf_counter() - t0
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"[HTTP ERROR] {e}")
        except (ValueError, KeyError) as e:
            raise RuntimeError(f"[PARSE ERROR] {e}")
