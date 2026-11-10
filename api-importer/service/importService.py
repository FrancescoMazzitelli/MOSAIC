from concurrent.futures import ThreadPoolExecutor
import requests
import yaml
import prance          # libreria per risolvere i $ref negli OpenAPI YAML
import os
import json
import re
import random
from urllib.parse import quote, unquote, urlparse
import time


class Service:
    # -----------------------------------------------------------------------
    # Variabili d'ambiente: permettono di configurare gli host senza
    # modificare il codice (cambiano tra sviluppo locale e Docker)
    # -----------------------------------------------------------------------
    CONSUL_HOST = os.environ.get("CONSUL_HOST", "registry")
    CONSUL_PORT = int(os.environ.get("CONSUL_PORT", 8500))

    GATEWAY_HOST = os.environ.get("GATEWAY_HOST", "catalog-gateway")
    GATEWAY_PORT = int(os.environ.get("GATEWAY_PORT", 5000))

    HEALTHCHECK_SERVICE_HOST = os.environ.get("HEALTHCHECK_SERVICE_HOST", "healthcheck-service")
    HEALTHCHECK_SERVICE_PORT = os.environ.get("HEALTHCHECK_SERVICE_PORT", 5600)

    MOCK_SERVER_URL = os.environ.get("MOCK_SERVER_URL", "http://mock-server:8080")

    # -----------------------------------------------------------------------
    # Metodi per importare API esterne da apis.guru
    # -----------------------------------------------------------------------

    def fetch_providers(self):
        # recupera la lista di tutti i provider da apis.guru
        url = "https://api.apis.guru/v2/providers.json"
        response = requests.get(url)
        response.raise_for_status()
        return response.json()["data"]

    def fetch_api_details(self, provider):
        # recupera i dettagli di un provider specifico (versioni, URL YAML, ecc.)
        url = f"https://api.apis.guru/v2/{provider}.json"
        response = requests.get(url)
        response.raise_for_status()
        return response.json()

    def extract_swagger_url(self, api_data, provider):
        # cerca l'URL dello YAML OpenAPI nei metadati del provider
        # apis.guru può avere strutture diverse (dict o list) — gestisce entrambe
        apis = api_data.get("apis")

        if isinstance(apis, dict):
            for version_key, version_data in apis.items():
                swagger_url = version_data.get("swaggerYamlUrl")
                if swagger_url:
                    return swagger_url

        elif isinstance(apis, list):
            for entry in apis:
                if isinstance(entry, dict):
                    swagger_url = entry.get("swaggerYamlUrl") or entry.get("swaggerUrl")
                    if swagger_url:
                        return swagger_url

        print(f"[WARN] swaggerYamlUrl not found for provider {provider}")
        return None

    def extract_endpoints_from_swagger(self, swagger_url):
        # estrae solo i path (es. /bin, /bin/{id}) senza processare i dettagli
        # usato per analisi rapida, non per l'estrazione degli schema
        response = requests.get(swagger_url)
        if response.status_code != 200:
            return []
        try:
            swagger_data = yaml.safe_load(response.text)
            return list(swagger_data.get("paths", {}).keys())
        except yaml.YAMLError:
            return []

    # -----------------------------------------------------------------------
    # Helpers per estrazione schema — dotted keys con tipi compatti
    # Questi metodi sono il cuore del sistema di chaining:
    # trasformano uno schema OpenAPI complesso in una stringa compatta
    # che l'LLM può leggere e usare per costruire i placeholder corretti
    # -----------------------------------------------------------------------

    def _map_type(self, prop_schema):
        """
        Converte il tipo OpenAPI di una singola property nel tipo compatto
        usato nella stringa schema passata all'LLM.

        OpenAPI usa "type: integer", "type: string", ecc.
        Noi usiamo: int, float, str, bool, enum, arr, obj, any
        
        Esempio: {"type": "string", "enum": ["open","closed"]} → "enum"
                 {"type": "integer"} → "int"
        """
        if not isinstance(prop_schema, dict):
            return "any"

        # enum ha priorità su type: anche "type: string" con enum è "enum"
        if prop_schema.get("enum"):
            return "enum"

        oa_type = prop_schema.get("type", "")

        if oa_type == "integer":
            return "int"
        if oa_type == "number":
            return "float"
        if oa_type == "boolean":
            return "bool"
        if oa_type == "array":
            return "arr"
        if oa_type == "string":
            return "str"
        # oggetto con properties annidate → "obj" (ma _flatten_dotted lo espande)
        if oa_type == "object" or "properties" in prop_schema or "allOf" in prop_schema:
            return "obj"

        return "any"

    def _flatten_dotted(self, schema, prefix=""):
        """
        Riceve uno schema OpenAPI già dereferenziato da prance (tutti i $ref
        sono stati sostituiti con il loro contenuto reale) e restituisce
        un dizionario piatto con le dotted keys.

        Esempio di input (schema Bin dopo prance):
          {allOf: [{properties: {location: {type: str}, fillLevel: {type: int}}},
                   {properties: {id: {type: int}}}]}

        Esempio di output:
          {"location": "str", "fillLevel": "int", "id": "int"}

        Il prefix viene usato per oggetti annidati:
          se "address" ha dentro "street" e "city",
          il risultato è {"address.street": "str", "address.city": "str"}
        """
        if not isinstance(schema, dict):
            return {}

        result = {}

        # --- allOf: pattern usato per l'ereditarietà in OpenAPI ---
        # Es: Bin = allOf[NewBin, {properties: {id: int}}]
        # Itera tutti i sotto-schemi e unisce i risultati
        for sub in schema.get("allOf", []):
            result.update(self._flatten_dotted(sub, prefix))

        # --- anyOf / oneOf: schemi polimorfici ---
        # Invece di scegliere solo una variante, le unisce tutte
        # per non perdere campi che potrebbero essere presenti
        for combinator in ("anyOf", "oneOf"):
            for sub in schema.get(combinator, []):
                result.update(self._flatten_dotted(sub, prefix))

        # --- properties dirette: il caso più comune ---
        for prop_name, prop_schema in schema.get("properties", {}).items():
            if not isinstance(prop_schema, dict):
                prop_schema = {}

            full_key = f"{prefix}{prop_name}"  # es: "address.street"

            # ricorre per oggetti annidati (es. prop_schema ha a sua volta properties)
            sub_props = self._flatten_dotted(prop_schema, f"{full_key}.")
            if sub_props:
                # oggetto annidato → espande con dotted keys
                result.update(sub_props)
            else:
                # foglia: mappa al tipo compatto
                result[full_key] = self._map_type(prop_schema)

        # --- array: entra solo se non ha già trovato properties ---
        # (if not result evita di sovrascrivere risultati già trovati)
        if not result and schema.get("type") == "array":
            items = schema.get("items", {})

            # Guardia: in API esterne (apis.guru) items può essere una lista
            # (tuple tipizzate OpenAPI 3.1) o una stringa malformata.
            # In quei casi non possiamo scendere — restituiamo quello che abbiamo.
            if not isinstance(items, dict):
                return result

            # distingue array di oggetti complessi da array di scalari
            has_object_items = (
                items.get("type") == "object" or
                "properties" in items or
                "allOf" in items
            )

            if has_object_items:
                field_name = prefix.rstrip(".")
                if field_name:
                    # CASO 1: Array annidato dentro un altro oggetto -> lo marchiamo come "arr"
                    result[field_name] = "arr"
                else:
                    # CASO 2: Array alla ROOT (la risposta API è una lista di oggetti).
                    # Dobbiamo scendere dentro 'items' ed estrarre i campi!
                    sub = self._flatten_dotted(items, prefix)
                    if sub:
                        result.update(sub)
            else:
                # array di scalari (es. availableLanguages: ["it", "en"])
                sub = self._flatten_dotted(items, prefix)
                if sub:
                    result.update(sub)
                else:
                    # scalare semplice (es. array of string): rappresenta come "arr"
                    field_name = prefix.rstrip(".")
                    if field_name:
                        result[field_name] = "arr"

        return result

    def _infer_type_from_value(self, value):
        """
        Inferisce il tipo compatto da un valore concreto di un example.
        Usata dal fallback quando lo schema OpenAPI è assente.

        Esempio: value=42 → "int", value="open" → "str", value=[...] → "arr"
        """
        if isinstance(value, bool):
            return "bool"
        if isinstance(value, int):
            return "int"
        if isinstance(value, float):
            return "float"
        if isinstance(value, str):
            return "str"
        if isinstance(value, list):
            return "arr"
        if isinstance(value, dict):
            return "obj"
        return "any"

    def _flatten_dotted_from_example(self, example_value, prefix=""):
        """
        Versione alternativa di _flatten_dotted che lavora su un valore
        di esempio concreto invece che su uno schema OpenAPI.

        Usata come fallback quando lo schema è assente o vuoto.
        Inferisce i tipi dai valori reali invece che dalle dichiarazioni.

        Esempio di input (primo elemento di GET /bin):
          {"id": 1, "location": "City Square", "fillLevel": 35, "status": "normal"}

        Esempio di output:
          {"id": "int", "location": "str", "fillLevel": "int", "status": "str"}
        """
        if isinstance(example_value, dict):
            result = {}
            for key, val in example_value.items():
                full_key = f"{prefix}{key}"
                if isinstance(val, dict):
                    # oggetto annidato: ricorre con dotted prefix
                    sub = self._flatten_dotted_from_example(val, f"{full_key}.")
                    result.update(sub) if sub else result.update({full_key: "obj"})
                else:
                    result[full_key] = self._infer_type_from_value(val)
            return result

        elif isinstance(example_value, list) and example_value:
            # per gli array, analizza solo il primo elemento come rappresentativo
            return self._flatten_dotted_from_example(example_value[0], prefix)

        return {}

    def _schema_to_string(self, flat_dict, is_array):
        """
        Converte il dizionario piatto {campo: tipo} nella stringa compatta
        che viene salvata in MongoDB e passata all'LLM nel prompt.

        is_array=False → "{id:int, location:str, fillLevel:int, status:enum}"
        is_array=True  → "[{id:int, location:str, fillLevel:int, status:enum}]"

        Le parentesi quadre indicano all'LLM che la risposta è un array,
        quindi dovrà usare l'indice o FIND per accedere agli elementi.
        """
        if not flat_dict:
            return None
        inner = ", ".join(f"{k}:{v}" for k, v in flat_dict.items())
        return f"[{{{inner}}}]" if is_array else f"{{{inner}}}"

    def _collect_required_fields(self, schema):
        """
        Raccoglie i nomi di tutti i campi required da uno schema.
        Gestisce anche allOf perché il pattern comune in OpenAPI è:

          NewBin:
            allOf:
              - $ref: BaseModel   (che ha i suoi required)
              - properties: {id}
                required: [id]    (required aggiuntivi)

        Ritorna un set di stringhe con i nomi dei campi obbligatori.
        Usata da _extract_request_schema_from_details per aggiungere il marker *
        """
        required = set(schema.get("required", []))
        # raccoglie anche i required dai sotto-schemi di allOf
        for sub in schema.get("allOf", []):
            required.update(sub.get("required", []))
        return required

    def _extract_schema_from_details(self, details):
        """
        Estrae lo schema della risposta di successo (200 o 201) di un endpoint.
        Usata per costruire i RESPONSE SCHEMAS passati all'LLM.

        Strategia a due livelli:
          1. Schema OpenAPI dichiarativo (priorità massima — è la "verità contrattuale")
          2. Examples come fallback (quando lo schema manca o è vuoto)

        HTTP 204 è escluso perché non ha body (DELETE restituisce 204).

        Output: stringa tipo "[{id:int, location:str, status:enum}]"
                oppure None se non riesce a estrarre nulla
        """
        responses = details.get("responses", {})

        # cerca la risposta di successo: 200 (GET/PUT) o 201 (POST)
        # 204 (DELETE) non ha body → escluso intenzionalmente
        success_response = None
        for code in ["200", "201", 200, 201]:
            if code in responses:
                success_response = responses[code]
                break

        if not success_response or not isinstance(success_response, dict):
            return None

        # supporta sia Swagger 2.0 che OpenAPI 3.0 (strutture diverse)
        swagger2_schema   = success_response.get("schema", {})
        swagger2_examples = success_response.get("examples", {})
        swagger2_json_ex  = swagger2_examples.get("application/json") if isinstance(swagger2_examples, dict) else None

        content      = success_response.get("content", {})
        json_content = content.get("application/json", {})

        # --- Strategia 1: Schema dichiarativo (source of truth) ---
        # preferisce lo schema OpenAPI 3.0, poi il Swagger 2.0
        final_schema = json_content.get("schema") or swagger2_schema

        if isinstance(final_schema, dict) and final_schema:
            is_array  = final_schema.get("type") == "array"
            flat_dict = self._flatten_dotted(final_schema)
            if flat_dict:
                return self._schema_to_string(flat_dict, is_array)

        # --- Strategia 2: Examples come fallback ---
        # usata quando lo schema è assente o non ha properties

        # 2a. OpenAPI 3.0 — examples (plurale, dizionario di esempi nominati)
        examples = json_content.get("examples", {})
        if examples:
            # prende il primo esempio disponibile
            first_example = next(iter(examples.values()), None)
            if isinstance(first_example, dict) and first_example.get("value") is not None:
                ex_val    = first_example["value"]
                is_array  = isinstance(ex_val, list)
                flat_dict = self._flatten_dotted_from_example(ex_val)
                if flat_dict:
                    return self._schema_to_string(flat_dict, is_array)

        # 2b. OpenAPI 3.0 — example (singolare)
        if "example" in json_content and json_content["example"] is not None:
            ex_val    = json_content["example"]
            is_array  = isinstance(ex_val, list)
            flat_dict = self._flatten_dotted_from_example(ex_val)
            if flat_dict:
                return self._schema_to_string(flat_dict, is_array)

        # 2c. Swagger 2.0 — esempio JSON esplicito
        if swagger2_json_ex is not None:
            is_array  = isinstance(swagger2_json_ex, list)
            flat_dict = self._flatten_dotted_from_example(swagger2_json_ex)
            if flat_dict:
                return self._schema_to_string(flat_dict, is_array)

        return None

    def _extract_request_schema_from_details(self, details, method):
        """
        Estrae lo schema del body di input per POST e PUT.
        GET e DELETE non hanno body → restituisce None direttamente.

        I campi required sono marcati con * nel formato output,
        così l'LLM sa quali campi deve obbligatoriamente includere
        nel body della richiesta.

        Output: "{location:str*, fillLevel:int*, binType:enum*, status:enum*}"
                (i campi senza * sono opzionali)
        """
        # POST, PUT e PATCH hanno un request body
        if method.upper() not in {"POST", "PUT", "PATCH"}:
            return None

        schema = None

        # OpenAPI 3.0: lo schema sta in requestBody.content.application/json.schema
        request_body = details.get("requestBody", {})
        if isinstance(request_body, dict):
            content      = request_body.get("content", {})
            json_content = content.get("application/json", {})
            schema       = json_content.get("schema")

        # Swagger 2.0: lo schema sta in parameters[in=body].schema
        if not schema:
            for param in details.get("parameters", []):
                if isinstance(param, dict) and param.get("in") == "body":
                    schema = param.get("schema")
                    break

        if not schema or not isinstance(schema, dict):
            return None

        # raccoglie i campi required (gestisce anche allOf ricorsivamente)
        required_fields = self._collect_required_fields(schema)

        flat_dict = self._flatten_dotted(schema)
        if not flat_dict:
            return None

        # serializza aggiungendo * sui campi required
        parts = []
        for key, type_label in flat_dict.items():
            # per dotted keys (es. "address.street"), controlla solo la parte root
            # perché il required è dichiarato al livello dell'oggetto padre
            root_field = key.split(".")[0]
            marker = "*" if root_field in required_fields else ""
            parts.append(f"{key}:{type_label}{marker}")

        inner = ", ".join(parts)
        return f"{{{inner}}}"

    def _extract_parameters_from_details(self, details):
        """
        Estrae query parameter e path parameter da un endpoint.
        Ritorna una stringa compatta, es: "{zoneId:str*, status:enum}"
        """
        params = details.get("parameters", [])
        if not params:
            return None

        extracted = {}
        for p in params:
            if isinstance(p, dict) and p.get("in") in ["query", "path"]:
                name = p.get("name")
                is_required = p.get("required", False)
                schema = p.get("schema", {})
                
                # Usa lo stesso _map_type che usi per il resto
                param_type  = self._map_type(schema)
                marker      = "*" if is_required or p.get("in") == "path" else ""

                # Include i valori enum in formato compatto: enum(v1,v2,v3)
                # Risparmia token rispetto a descrivere i valori in prosa,
                # ed evita che l'LLM inventi varianti (es: airQuality vs air_quality)
                # Cap a 8 valori per non gonfiare il prompt su enum molto grandi
                enum_values = schema.get("enum")
                if enum_values and isinstance(enum_values, list) and len(enum_values) <= 8:
                    type_label = "enum(" + ",".join(str(v) for v in enum_values) + ")"
                else:
                    type_label = param_type

                extracted[name] = f"{type_label}{marker}"

        if not extracted:
            return None

        # Formatta come JSON-like compatto (TOON)
        inner = ", ".join(f"{k}:{v}" for k, v in extracted.items())
        return f"{{{inner}}}"

    def _build_swagger_url_from_endpoint(self, endpoint_url, mock_server_url):
        """
        Ricostruisce l'URL dello YAML Microcks a partire da un endpoint
        già salvato in MongoDB.

        Serve come fallback quando swagger_url non è stato salvato direttamente
        (es. servizi registrati prima che venisse aggiunto il campo swagger_url).

        Esempio di trasformazione:
          input:  "http://localhost:8585/rest/Smart+Bins+API/1.0/bin"
          output: "http://mock-server:8080/api/resources/Smart%20Bins%20API-1.0.yaml"

        Il mock server Microcks espone gli YAML originali sotto /api/resources/
        con il formato: {NomeAPI}-{versione}.yaml (spazi codificati come %20)
        """
        # estrae la parte dopo /rest/
        match = re.search(r"/rest/(.+)", endpoint_url)
        if not match:
            return None

        rest_path = match.group(1)
        parts     = rest_path.split("/")
        if len(parts) < 2:
            return None

        api_name_raw     = parts[0]            # es: "Smart+Bins+API"
        version          = parts[1]            # es: "1.0"
        api_name         = unquote(api_name_raw.replace("+", " "))  # → "Smart Bins API"
        api_name_encoded = quote(api_name)     # → "Smart%20Bins%20API"

        filename = f"{api_name_encoded}-{version}.yaml"
        return f"{mock_server_url.rstrip('/')}/api/resources/{filename}"

    def _generate_description(self, swagger: dict) -> str:
        """
        Genera una description sintetica per l'indexing Stage 1 del retrieval.

        Costruita da:
          - Titolo dell'API (ancora semantica)
          - Summary di ogni endpoint (testo funzionale, più denso della description)
          - Valori enum dei parametri (vocabolario di dominio esatto)

        La description originale (info.description) è spesso astratta e ha
        overlap lessicale basso con le query utente. I summary e gli enum
        contengono invece i termini usati nelle query: "Find best available
        parking", "spotType: standard, electric, disabled", ecc.

        Viene salvata nel campo generated_description (separato da description)
        e usata esclusivamente per l'indexing in Qdrant, non inviata all'LLM.
        """
        info  = swagger.get("info", {})
        paths = swagger.get("paths", {})
        parts = []

        title = info.get("title", "").strip()
        if title:
            parts.append(title)

        seen_summaries = set()
        for path, methods in paths.items():
            if not isinstance(methods, dict):
                continue
            for method, details in methods.items():
                if method.lower() not in {"get", "post", "put", "delete", "patch"}:
                    continue
                if not isinstance(details, dict):
                    continue
                summary = details.get("summary", "").strip()
                if summary and summary not in seen_summaries:
                    parts.append(summary)
                    seen_summaries.add(summary)

        seen_params = set()
        for path, methods in paths.items():
            if not isinstance(methods, dict):
                continue
            for method, details in methods.items():
                if not isinstance(details, dict):
                    continue
                for param in details.get("parameters", []):
                    if not isinstance(param, dict):
                        continue
                    schema     = param.get("schema", {})
                    enum_vals  = schema.get("enum", [])
                    param_name = param.get("name", "").strip()
                    if enum_vals and param_name and param_name not in seen_params:
                        enum_str = ", ".join(str(v) for v in enum_vals[:10])
                        parts.append(f"{param_name}: {enum_str}")
                        seen_params.add(param_name)

        if not parts:
            return info.get("description", "").strip()

        return ". ".join(parts)

    def _extract_schemas_from_yaml(self, swagger_url):
        try:
            # --- FIX UTF-8 ---
            resp = requests.get(swagger_url)
            resp.encoding = 'utf-8'  # Forza la decodifica dei caratteri speciali (come "—")
            parser = prance.ResolvingParser(spec_string=resp.text, lazy=False, strict=False)
            # -----------------
            swagger = parser.specification
        except Exception as e:
            print(f"Failed to load/resolve YAML from {swagger_url}: {e}")
            return {"response_schemas": {}, "request_schemas": {}, "parameters": {}}

        response_schemas = {}
        request_schemas  = {}
        parameters       = {}

        paths = swagger.get("paths", {})
        for path, methods in paths.items():
            if not isinstance(methods, dict):
                continue
            for method, details in methods.items():
                if method.lower() not in {"get", "post", "put", "delete", "patch"}:
                    continue
                if not isinstance(details, dict):
                    continue

                key = f"{method.upper()} {path}"

                resp_schema = self._extract_schema_from_details(details)
                if resp_schema:
                    response_schemas[key] = resp_schema

                req_schema = self._extract_request_schema_from_details(details, method)
                if req_schema:
                    request_schemas[key] = req_schema

                params_schema = self._extract_parameters_from_details(details)
                if params_schema:
                    parameters[key] = params_schema

        # Genera la description arricchita dallo stesso swagger già parsato.
        # Nessun secondo download — usa parser.specification già in memoria.
        generated_description = self._generate_description(swagger)

        return {
            "response_schemas":      response_schemas,
            "request_schemas":       request_schemas,
            "parameters":            parameters,
            "generated_description": generated_description,
        }
    # -----------------------------------------------------------------------
    # Metodo principale di enrichment
    # -----------------------------------------------------------------------

    def enrich_schemas(self, mock_server_url=None, service_id=None):
        """
        Punto di ingresso dell'enrichment: chiamato da mock-deployer dopo
        il deploy dei servizi tramite POST /api/importer/enrich.

        Flusso:
          1. Recupera tutti i servizi da MongoDB via catalog-gateway
          2. Per ognuno, trova l'URL dello YAML (da swagger_url o ricostruendolo)
          3. Estrae gli schema con _extract_schemas_from_yaml
          4. Salva in MongoDB via PATCH /services/{id}/schemas

        service_id opzionale: se specificato, arricchisce solo quel servizio.
        """
        mock_server_url = mock_server_url or self.MOCK_SERVER_URL
        gateway_base    = f"http://{self.GATEWAY_HOST}:{self.GATEWAY_PORT}"

        try:
            resp = requests.get(f"{gateway_base}/services", timeout=10)
            resp.raise_for_status()
            all_services = resp.json()
        except Exception as e:
            print(f"[ENRICH] Cannot fetch services from gateway: {e}")
            return {"enriched": 0, "skipped": 0, "errors": 1}

        # filtro opzionale per singolo servizio
        if service_id:
            all_services = [s for s in all_services if s.get("_id") == service_id]

        enriched = 0
        skipped  = 0
        errors   = 0

        for svc in all_services:
            doc_id = svc.get("_id")

            # skip idempotente: non rielabora servizi già arricchiti
            # (evita di sovrascrivere schema corretti con una riesecuzione)
            if ("response_schemas" in svc and "request_schemas" in svc
                    and "parameters" in svc and "generated_description" in svc):
                print(f"[ENRICH] Skipping {doc_id} (already enriched)")
                skipped += 1
                continue

            # trova l'URL dello YAML: prima cerca il campo diretto in MongoDB,
            # poi tenta la ricostruzione dall'endpoint come fallback
            swagger_url = svc.get("swagger_url")

            if not swagger_url:
                endpoints = svc.get("endpoints", {})
                for ep_url in endpoints.values():
                    if isinstance(ep_url, str) and "/rest/" in ep_url:
                        swagger_url = self._build_swagger_url_from_endpoint(ep_url, mock_server_url)
                        break

            if not swagger_url:
                print(f"[ENRICH] Cannot find or reconstruct swagger URL for {doc_id}")
                skipped += 1
                continue

            # estrae response_schemas e request_schemas dallo YAML
            schemas = self._extract_schemas_from_yaml(swagger_url)

            try:
                # aggiorna il documento MongoDB con gli schema estratti
                # PATCH invece di PUT: aggiunge i campi senza sovrascrivere il resto
                patch_resp = requests.patch(
                    f"{gateway_base}/services/{doc_id}/schemas",
                    json=schemas,
                    timeout=10
                )
                patch_resp.raise_for_status()
                n_resp   = len(schemas.get("response_schemas", {}))
                n_req    = len(schemas.get("request_schemas", {}))
                n_params = len(schemas.get("parameters", {}))
                has_desc = bool(schemas.get("generated_description"))
                print(f"[ENRICH] {doc_id} → {n_resp} response, {n_req} request, {n_params} parameters, generated_description={'yes' if has_desc else 'no'}")
                enriched += 1
            except Exception as e:
                print(f"[ENRICH] Failed to update {doc_id}: {e}")
                errors += 1

        return {"enriched": enriched, "skipped": skipped, "errors": errors}

    # -----------------------------------------------------------------------
    # Metodi originali di registrazione
    # Usati da import_apis() per i servizi esterni da apis.guru
    # parse_swagger è analogo a _extract_schemas_from_yaml ma include
    # anche capabilities, endpoints e swagger_url nel payload di ritorno
    # -----------------------------------------------------------------------

    def parse_swagger(self, service, swagger_url, fallback_base_url=None):
        """
        Analizza uno YAML OpenAPI e costruisce il documento completo
        da salvare in MongoDB per un servizio.

        A differenza di _extract_schemas_from_yaml (che estrae solo gli schema),
        questo metodo estrae anche:
          - capabilities: descrizioni testuali degli endpoint (usate da Qdrant)
          - endpoints: URL completi degli endpoint
          - swagger_url: URL dello YAML (per futuri re-enrichment)
        """
        try:
            # --- FIX UTF-8 ---
            # /import/file passa un URL file://: requests non supporta lo
            # schema file, quindi leggiamo direttamente dal filesystem.
            if swagger_url.startswith("file://"):
                with open(swagger_url[len("file://"):], "r", encoding="utf-8") as fh:
                    spec_text = fh.read()
            else:
                resp = requests.get(swagger_url)
                resp.encoding = 'utf-8'
                spec_text = resp.text
            parser  = prance.ResolvingParser(
                spec_string=spec_text,
                lazy=False,
                strict=False,
            )
            # -----------------
            swagger = parser.specification
        except Exception as e:
            print(f"Failed to load/resolve YAML from {swagger_url}: {e}")
            return None

        try:
            info         = swagger.get("info", {})
            service_name = info.get("title", service)
            paths        = swagger.get("paths", {})
            servers      = swagger.get("servers")

            # --- LOGICA ORIGINALE ---
            if servers and isinstance(servers, list) and len(servers) > 0 and isinstance(servers[0], dict):
                host_url = servers[0].get("url", "http://localhost")
            else:
                host      = swagger.get("host", "localhost")
                schemes   = swagger.get("schemes", ["http"])
                base_path = swagger.get("basePath", "")
                scheme    = schemes[0] if isinstance(schemes, list) and schemes else "http"
                host_url  = f"{scheme}://{host}{base_path}"

            # --- NUOVA LOGICA DI FALLBACK (LA MAGIA) ---
            # Se dopo i controlli standard siamo ancora bloccati su localhost...
            if host_url.startswith("http://localhost"):
                # 1. Priorità al parametro manuale (se fornito)
                if fallback_base_url:
                    host_url = fallback_base_url.rstrip('/')
                # 2. Altrimenti deducilo dall'URL di download (se è un link remoto)
                elif swagger_url.startswith("http"):
                    parsed = urlparse(swagger_url)
                    host_url = f"{parsed.scheme}://{parsed.netloc}"

        except Exception as e:
            print(f"Failed to get information from parsed swagger: {e}")
            return None

        capabilities     = {}
        endpoints        = {}
        response_schemas = {}
        request_schemas  = {}
        parameters       = {} # <--- NUOVO DIZIONARIO

        for path, methods in paths.items():
            if not isinstance(methods, dict):
                continue
            for method, details in methods.items():
                if method.lower() not in {"get", "post", "put", "delete", "patch"}:
                    continue
                if not isinstance(details, dict):
                    continue

                key      = f"{method.upper()} {path}"
                desc     = details.get("description") or details.get("summary") or details.get("operationId") or method
                full_url = f"{host_url.rstrip('/')}{path}"

                capabilities[key] = desc
                endpoints[key]    = full_url

                resp_schema = self._extract_schema_from_details(details)
                if resp_schema:
                    response_schemas[key] = resp_schema

                req_schema = self._extract_request_schema_from_details(details, method)
                if req_schema:
                    request_schemas[key] = req_schema

                # --- NUOVA CHIAMATA PER I PARAMETRI ---
                params_schema = self._extract_parameters_from_details(details)
                if params_schema:
                    parameters[key] = params_schema

        # restituisce il documento completo pronto per MongoDB
        return {
            "id":                    service,
            "name":                  service_name,
            "description":           swagger.get("info", {}).get("description", "No description"),
            "generated_description": self._generate_description(swagger),
            "swagger_url":           swagger_url,
            "capabilities":          capabilities,
            "endpoints":             endpoints,
            "response_schemas":      response_schemas,
            "request_schemas":       request_schemas,
            "parameters":            parameters,
        }

    def register_to_redis(self, service_name, service_status):
        # registra lo stato di health del servizio in Redis
        # usato dall'healthcheck-service per sapere se il servizio è attivo
        headers      = {"Content-Type": "application/json"}
        json_payload = {"key": service_name, "value": service_status}
        response = requests.post(
            f"http://{self.HEALTHCHECK_SERVICE_HOST}:{self.HEALTHCHECK_SERVICE_PORT}/status/register",
            headers=headers,
            data=json.dumps(json_payload)
        )
        if response.status_code == 200:
            print(f"Registered {service_name} to Redis with status {service_status}")
        else:
            print(f"Failed to register {service_name} to Redis: HTTP {response.status_code}")

    def register_to_consul(self, service_id, service_name):
        # registra il servizio in Consul con un health check automatico
        # Consul usa questo per sapere se il servizio è raggiungibile
        # e per escluderlo dal catalogo se non risponde
        payload = {
            "Name": service_name,
            "Id":   service_id,
            "Meta": {"service_doc_id": service_id},
            "Check": {
                "TlsSkipVerify":                 True,
                "Method":                         "GET",
                "Http":                           f"http://{self.HEALTHCHECK_SERVICE_HOST}:{self.HEALTHCHECK_SERVICE_PORT}/status/{service_id}",
                "Interval":                       "10s",   # controlla ogni 10 secondi
                "Timeout":                        "5s",
                "DeregisterCriticalServiceAfter": "30s"    # rimuove se non risponde per 30s
            }
        }
        try:
            url      = f"http://{self.CONSUL_HOST}:{self.CONSUL_PORT}/v1/agent/service/register"
            response = requests.put(url, json=payload)
            print(f"Consul registration ({service_id}): HTTP {response.status_code}")
        except Exception as e:
            print(f"Failed to register to Consul: {e}")

    def _verify_service_in_mongo(self, service_id, retries=5, delay_sec=2.0):
        verify_url = f"http://{self.GATEWAY_HOST}:{self.GATEWAY_PORT}/services/{service_id}"
        for attempt in range(1, retries + 1):
            try:
                r = requests.get(verify_url, timeout=20)
                if r.status_code == 200:
                    return True
                print(f"[MONGO][VERIFY][attempt {attempt}/{retries}] HTTP {r.status_code} for id={service_id}")
            except Exception as e:
                print(f"[MONGO][VERIFY][attempt {attempt}/{retries}] error for id={service_id}: {e}")
            time.sleep(delay_sec)
        return False

    def register_to_mongo(self, catalog_payload):
        service_id = catalog_payload.get("id", "UNKNOWN")
        post_url = f"http://{self.GATEWAY_HOST}:{self.GATEWAY_PORT}/service"

        # Il gateway calcola un embedding per OGNI capability su CPU: i servizi
        # apis.guru con molti endpoint possono richiedere ben più di 15s. Timeout
        # generoso e configurabile per evitare falsi [MONGO][FAILED] (il dato in
        # realtà arriva, ma il client mollava troppo presto).
        mongo_timeout = int(os.environ.get("MONGO_REGISTER_TIMEOUT", "180"))
        try:
            print(f"[MONGO][ATTEMPT] POST {post_url} id={service_id}")
            response = requests.post(post_url, json=catalog_payload, timeout=mongo_timeout)
            response.raise_for_status()
            print(f"[MONGO][HTTP_OK] id={service_id} HTTP {response.status_code}")

            if service_id == "UNKNOWN":
                raise RuntimeError("[MONGO][INCONSISTENT] Missing 'id' in payload, cannot verify persistence")

            if not self._verify_service_in_mongo(service_id):
                raise RuntimeError(f"[MONGO][INCONSISTENT] HTTP success but service not found after write id={service_id}")

            print(f"[MONGO][VERIFIED] id={service_id} persisted in Mongo")
            return True

        except requests.exceptions.RequestException as e:
            status = e.response.status_code if getattr(e, "response", None) is not None else "N/A"
            body = e.response.text if getattr(e, "response", None) is not None else ""
            raise RuntimeError(f"[MONGO][FAILED] id={service_id} HTTP={status} body={body}") from e

    def import_apis(self, limit=None, seed=42, offset=0):
        """
        Importa i servizi pubblici da apis.guru.
        Per ogni provider: scarica lo YAML, estrae capabilities/endpoints/schema,
        poi registra in parallelo su Redis, Consul e MongoDB.

        Usato per arricchire il catalogo con servizi reali esterni
        (non i mock Smart City locali).

        Parametri (per il test di robustezza della discovery):
            limit:  indice di fine del campione (esclusivo). None = tutti.
            seed:   seme per il mescolamento deterministico. A parità di seme,
                    i sottoinsiemi sono NIDIFICATI (limit=50 ⊂ limit=100 ⊂ ...),
                    così la curva a gradini aggiunge solo distrattori nuovi.
            offset: indice di inizio del campione. Permette di importare SOLO
                    la fetta nuova [offset:limit] a ogni gradino, evitando di
                    ri-scaricare e ri-registrare i provider già presenti.
        """
        all_endpoints = {}
        providers     = self.fetch_providers()
        print(f"Found {len(providers)} providers.")

        # Campionamento riproducibile e nidificato per la curva a gradini.
        # sorted() rende stabile l'ordine di partenza a prescindere dall'ordine
        # restituito da apis.guru; lo shuffle con seme fisso dà diversità di
        # dominio (evita di prendere solo i provider 'a.../b...').
        # rank_map: provider -> sample_rank assoluto nell'ordine deterministico.
        # Serve a taggare ogni distrattore per il filtro max_rank del gateway.
        rank_map = {}
        if limit is not None or offset:
            providers = sorted(providers)
            random.Random(seed).shuffle(providers)
            start = max(0, int(offset))
            end   = max(start, int(limit)) if limit is not None else len(providers)
            providers = providers[start:end]
            # L'elemento in posizione i della fetta ha rank assoluto start+i.
            rank_map = {p: start + i for i, p in enumerate(providers)}
            print(f"Importing providers [{start}:{end}] → {len(providers)} (seed={seed}).")

        for provider in providers:
            try:
                api_data    = self.fetch_api_details(provider)
                swagger_url = self.extract_swagger_url(api_data, provider)
                all_endpoints[provider] = swagger_url
            except Exception as e:
                print(f"Error processing {provider}: {e}")

        print(f"\nFound: {len(all_endpoints)} endpoints.")
        for service, swagger_url in all_endpoints.items():
            if not swagger_url:
                print(f"[SKIP] {service}: swagger URL not found.")
                continue

            openapi = self.parse_swagger(service, swagger_url)
            if not openapi:
                print(f"[SKIP] {service}: error parsing swagger.")
                continue

            # Tagga il distrattore con il suo rank deterministico (se disponibile).
            if service in rank_map:
                openapi["sample_rank"] = rank_map[service]

            # le tre registrazioni avvengono in parallelo con ThreadPoolExecutor
            # poi si aspetta che tutte finiscano prima di procedere (BARRIER)
            with ThreadPoolExecutor(max_workers=3) as executor:
                futures = [
                    executor.submit(self.register_to_redis, service, "true"),
                    executor.submit(self.register_to_consul, service, openapi.get("id", service)),
                    executor.submit(self.register_to_mongo, openapi)
                ]

                # BARRIER: aspetta tutti i thread prima di andare al prossimo servizio
                for future in futures:
                    try:
                        future.result()
                    except Exception as e:
                        print(f"[ERROR] Register function failed: {e}")

    def backfill_ranks(self, seed=42):
        """
        Assegna sample_rank ai distrattori GIÀ presenti in catalogo, senza
        riscaricare nulla da apis.guru.

        Ricalcola lo stesso ordine deterministico di import_apis (provider
        ordinati + shuffle con seed), poi per ogni provider già presente in
        catalogo chiama PATCH /services/<id>/rank sul gateway (aggiorna Mongo +
        payload Stage 1 senza ri-embedding).

        Ritorna {"tagged": n, "present": m, "errors": e}.
        """
        gateway_base = f"http://{self.GATEWAY_HOST}:{self.GATEWAY_PORT}"

        providers = sorted(self.fetch_providers())
        random.Random(seed).shuffle(providers)
        rank_map = {p: i for i, p in enumerate(providers)}

        # ID dei servizi attualmente in catalogo
        try:
            resp = requests.get(f"{gateway_base}/services", timeout=30)
            resp.raise_for_status()
            present_ids = {s.get("_id") for s in resp.json()}
        except Exception as e:
            print(f"[BACKFILL] Cannot fetch services: {e}")
            return {"tagged": 0, "present": 0, "errors": 1}

        tagged = errors = 0
        for sid in present_ids:
            if sid not in rank_map:
                continue  # non è un distrattore apis.guru (es. mock smart-city)
            try:
                r = requests.patch(
                    f"{gateway_base}/services/{sid}/rank",
                    json={"sample_rank": rank_map[sid]},
                    timeout=30,
                )
                r.raise_for_status()
                tagged += 1
            except Exception as e:
                print(f"[BACKFILL] Failed to tag {sid}: {e}")
                errors += 1

        print(f"[BACKFILL] tagged={tagged} present={len(present_ids)} errors={errors} (seed={seed})")
        return {"tagged": tagged, "present": len(present_ids), "errors": errors}

    def register_existing_to_registry(self):
        """
        Ri-registra in Consul + Redis tutti i servizi GIÀ in catalogo (Mongo) che
        non risultano nel registry, SENZA riscaricare/parsare nulla.

        Serve quando i servizi sono stati caricati via /import/file (come i 275
        SOCBench-D) ma non sono in Consul: in quel caso la control-unit li
        scarterebbe come 'orphaned' e l'end-to-end non vedrebbe i servizi.

        NOTA: a differenza di prima, questo metodo NON filtra su sample_rank,
        perché i servizi SOCBench-D non hanno questo campo. Vengono registrati
        tutti i servizi non già presenti in Consul.

        Il health check è dirottato su GET /health del catalog-gateway (che
        risponde sempre 200) invece di /status/{service_id} del
        healthcheck-service, che richiede che il servizio sia stato registrato
        in Redis con valore "true". Questo evita che i servizi vengano
        marcati come critici da Consul solo perché il mock server non è
        raggiungibile o perché il servizio non è stato ancora registrato in
        Redis.

        Ritorna {"registered": n, "skipped": m, "errors": e}.
        """
        gateway_base = f"http://{self.GATEWAY_HOST}:{self.GATEWAY_PORT}"

        try:
            resp = requests.get(f"{gateway_base}/services", timeout=30)
            resp.raise_for_status()
            services = resp.json()
        except Exception as e:
            print(f"[REGISTER] Cannot fetch services: {e}")
            return {"registered": 0, "skipped": 0, "errors": 1}

        # ID già presenti in Consul (per non duplicare)
        try:
            c = requests.get(
                f"http://{self.CONSUL_HOST}:{self.CONSUL_PORT}/v1/agent/services",
                timeout=10,
            )
            c.raise_for_status()
            consul_ids = set(c.json().keys())
        except Exception as e:
            print(f"[REGISTER] Cannot read Consul services: {e}")
            consul_ids = set()

        registered = skipped = errors = 0
        for s in services:
            sid = s.get("_id")
            # Registra TUTTI i servizi non già in Consul (non solo i distrattori)
            if not sid or sid in consul_ids:
                skipped += 1
                continue
            try:
                self.register_to_redis(sid, "true")
                # Health check su endpoint "fantoccio" che risponde sempre 200
                self.register_to_consul_with_dummy_healthcheck(sid, s.get("name", sid))
                registered += 1
            except Exception as e:
                print(f"[REGISTER] Failed {sid}: {e}")
                errors += 1

        print(f"[REGISTER] registered={registered} skipped={skipped} errors={errors}")
        return {"registered": registered, "skipped": skipped, "errors": errors}

    def register_to_consul_with_dummy_healthcheck(self, service_id, service_name):
        """
        Registra il servizio in Consul con un health check che punta a un
        endpoint "fantoccio" (GET /health del catalog-gateway) che risponde
        sempre 200, invece di /status/{service_id} del healthcheck-service che
        richiede che il servizio sia stato registrato in Redis con valore
        "true".

        Questo evita che i servizi vengano marcati come critici da Consul
        solo perché il mock server non è raggiungibile o perché il servizio
        non è stato ancora registrato in Redis.
        """
        payload = {
            "Name": service_name,
            "Id":   service_id,
            "Meta": {"service_doc_id": service_id},
            "Check": {
                "TlsSkipVerify":                 True,
                "Method":                         "GET",
                "Http":                           f"http://{self.GATEWAY_HOST}:{self.GATEWAY_PORT}/health",
                "Interval":                       "10s",
                "Timeout":                        "5s",
                "DeregisterCriticalServiceAfter": "30s"
            }
        }
        try:
            url      = f"http://{self.CONSUL_HOST}:{self.CONSUL_PORT}/v1/agent/service/register"
            response = requests.put(url, json=payload)
            print(f"Consul registration ({service_id}): HTTP {response.status_code}")
        except Exception as e:
            print(f"Failed to register to Consul: {e}")
            raise