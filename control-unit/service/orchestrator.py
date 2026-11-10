"""Orchestrator del Control Unit.

Questo modulo definisce la classe `Orchestrator` che coordina la pipeline
principale dell'unità di controllo: discovery -> planning -> execution.

La responsabilità principale dell'`Orchestrator` è:
- interrogare il catalogo per trovare servizi rilevanti per una query;
- filtrare i servizi effettivamente registrati nel registry;
- delegare la generazione del piano al `Planner`;
- validare e, se possibile, riparare il piano con `PlanValidator`;
- eseguire il piano tramite `Executor` e restituire i risultati.

Le risposte del metodo `control()` sono strutturate come dizionari con
chiavi quali `execution_plan`, `execution_results`, `error` e
`planning_latency_s`. In caso il risultato sia un file da scaricare,
viene restituito un oggetto `flask.Response`.
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
    """Coordina discovery, planning e execution.

    Attributi principali:
    - `model_name` (str): nome del modello LLM usato dal `Planner`.
    - `backend_mode` (str): modalità di backend ('MOCK' o 'REAL').
    - `planner` (Planner): istanza responsabile della generazione dei piani.
    - `executor` (Executor): istanza responsabile dell'esecuzione del piano.

    Il costruttore crea anche una cartella `Files` per il salvataggio
    temporaneo di file inviati dall'utente.
    """

    def __init__(self):
        self.model_name = os.environ.get("LLM_MODEL", "qwen3.5:27b")
        self.backend_mode = "MOCK"
        self.planner = Planner(self.model_name)
        self.executor = Executor()
        os.makedirs("Files", exist_ok=True)

    
    def analyze_files(self, files: list):
        """Analizza e salva file caricati dall'utente.

        Ogni file viene salvato nella cartella `Files` e viene restituito
        un elenco di dizionari con metadati utili al `Planner`.

        Args:
            files (list): lista di oggetti file (es. da Flask `request.files`).

        Returns:
            list: lista di dizionari con chiavi `filename`, `content_type`,
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
        """Sostituisce gli URL dei mock se in modalità `MOCK`.

        Alcuni servizi registrati nel catalogo possono puntare a
        `http://localhost:8585` (configurazione Microcks). Quando l'orchestrator
        gira in container, questa funzione riscrive quegli URL con
        `mock_server_address` in modo che i mock siano raggiungibili.

        Args:
            endpoints_list (list): lista di dizionari con endpoint.
            mock_server_address (str): indirizzo del mock server da usare.

        Returns:
            list: lista di endpoint con URL eventualmente riscritti.
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
        """Punto di ingresso principale per l'orchestrazione della richiesta.

        Flusso generale:
        1. Analizza eventuali file caricati.
        2. Recupera i servizi dal catalogo e filtra quelli registrati.
        3. Richiama il `Planner` per ottenere un piano di esecuzione.
        4. Valida (e tenta di riparare) il piano.
        5. Invia il piano all'`Executor` e raccoglie i risultati.

        Args:
            query (str): descrizione della richiesta dell'utente.
            files (list, optional): lista di file inviati dall'utente.
            max_rank (int, optional): limita i servizi restituiti dal catalogo.
            dry_run (bool): se True, si ferma dopo planning/validazione e non
                esegue il piano. Usato dall'harness di valutazione del Designer.

        Returns:
            dict | flask.Response: dizionario con `execution_plan`,
                `execution_results`, `error`, `planning_latency_s`, o un
                `flask.Response` se il risultato è un file da scaricare.
        """

        # Salva metadati dei file caricati (se presenti) per il planner
        analyzed_files = self.analyze_files(files or [])
        planning_latency_s = None

        # Metadati di routing usati dall'harness di valutazione. Sono additivi
        # e non cambiano il comportamento del sistema.
        route_reason = "UNKNOWN"
        planner_status = None
        planner_tasks_count = None
        validated_tasks_count = None
        designer_attempted = False

        # Determina la modalità di backend (MOCK o REAL)
        self.backend_mode = os.environ.get("BACKEND_MODE", "MOCK").upper()
        if self.backend_mode not in ("MOCK", "REAL"):
            # Se valore non riconosciuto, torniamo a MOCK per sicurezza
            print(f"[BACKEND_MODE] valore non riconosciuto '{self.backend_mode}', fallback su MOCK")
            self.backend_mode = "MOCK"
        print(f"[BACKEND_MODE] {self.backend_mode}")

        # Configurazioni esterne: URL dei servizi di catalogo/registry/mock
        catalog_url  = os.environ.get("CATALOG_URL",     "http://catalog-gateway:5000")
        registry_url = os.environ.get("REGISTRY_URL",    "http://registry:8500")
        mock_url     = os.environ.get("MOCK_SERVER_URL", "http://mock-server:8080")

        # Discovery: otteniamo la lista dei servizi dal registry
        registry     = Discovery(registry_url)
        services     = registry.services()
        # Interroghiamo il catalogo per trovare servizi rilevanti alla query.
        # max_rank (se presente) limita i distrattori considerati — usato dal
        # test di robustezza per simulare un catalogo con N distrattori.
        search_payload = {"query": query}
        if max_rank is not None:
            search_payload["max_rank"] = max_rank
        service_data = requests.post(f"{catalog_url}/index/search", json=search_payload).json()
        service_list = service_data.get("results", [])

        # Nessun servizio trovato -> risposta di errore esplicita
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

        # Filtriamo solo i servizi effettivamente registrati nel registry
        registry_ids          = {s["id"] for s in services}
        filtered_service_list = [s for s in service_list if s["_id"] in registry_ids]
        orphaned              = [s for s in service_list if s["_id"] not in registry_ids]
        if orphaned:
            # Log dei servizi presenti nel catalogo ma non nel registry
            print("[WARNING] Servizi non piu' nel registry:", [s.get("_id") for s in orphaned])
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

        # Stampa di dettaglio per debug/monitoring
        print("\n" + "=" * 60)
        print(f"[RETRIEVAL] Query: {query}")
        for s in filtered_service_list:
            caps = s.get("capabilities", {})
            print(f"  - {s.get('_id')} | {s.get('name')} | {len(caps)} endpoints")
            print(f"    {list(caps.keys())}")
        print("=" * 60 + "\n")

        # Normalizziamo e raccogliamo le informazioni utili dal catalogo
        disc_services, disc_caps, disc_eps = [], [], []
        disc_schemas, disc_req_schemas, disc_params = [], [], []
        for s in filtered_service_list:
            # Rimuoviamo l'endpoint di registrazione interno se presente
            if isinstance(s.get("capabilities"), dict): s["capabilities"].pop("POST /register", None)
            if isinstance(s.get("endpoints"), dict):    s["endpoints"].pop("POST /register", None)
            disc_services.append({"_id": s.get("_id"), "name": s.get("name"),
                                  "description": s.get("description")})
            disc_caps.append(s.get("capabilities", {}))
            disc_eps.append(s.get("endpoints", {})) 
            disc_schemas.append(s.get("response_schemas", {}))
            disc_req_schemas.append(s.get("request_schemas", {}))
            disc_params.append(s.get("parameters", {}))

        # Riscriviamo gli endpoint per i mock quando necessario con l'indirizzo del mock server raggiungibile dai container
        disc_eps = self.replace_endpoints(disc_eps, mock_url)

        # Struttura dati complessiva da passare al planner
        discovered = {
            "services": disc_services, "capabilities": disc_caps, "endpoints": disc_eps,
            "response_schemas": disc_schemas, "request_schemas": disc_req_schemas,
            "parameters": disc_params,
        }

        # Vista compatta dei servizi recuperati (ordinati come dal retrieval, già
        # filtrati per registry): id + capability. Esposta nella risposta così il
        # test può calcolare le metriche di retrieval su ciò che l'LLM ha visto,
        # senza dover scrapare i log.
        discovered_out = [
            {"_id": disc_services[i]["_id"],
             "name": disc_services[i]["name"],
             "capabilities": list((disc_caps[i] or {}).keys())}
            for i in range(len(disc_services))
        ]

        # ---- PLANNING ----
        # Chiediamo al planner di costruire un piano a partire dalla query
        plan, planning_latency_s, schema_warnings = self.planner.plan(
            query, discovered, analyzed_files, self.backend_mode)

        # Report delle eventuali violazioni di schema rilevate dal validator
        if schema_warnings:
            print(f"\n{'WARN ' * 10}")
            print(f"[SCHEMA VALIDATOR] {len(schema_warnings)} violazione/i rilevata/e:")
            for w in schema_warnings:
                print(f"  {w}")
            print(f"{'WARN ' * 10}\n")
        else:
            print("[SCHEMA VALIDATOR] Nessuna violazione rilevata.")

        # Stato del planner, indipendente da quello che succede dopo la
        # validazione. Serve all'harness per distinguere un piano non vuoto da
        # un JSON malformato o da un piano svuotato durante l'auto-fix.
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

        # ---- PIANO VUOTO -> Designer (triage + design) ----
        # Se il planner non propone alcuna azione, attiviamo il modulo di design
        if self.planner.is_empty(plan):
            print("\n[ROUTING] Piano vuoto rilevato. Attivazione Designer (triage + design)...")
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

        # ---- VALIDAZIONE + AUTO-FIX ----
        # Validiamo il piano rispetto ai servizi effettivamente disponibili
        available_ids = [s["_id"] for s in disc_services] # Lista degli ID dei servizi disponibili (dopo filtraggio registry)
        name_to_id    = {s.get("name"): s.get("_id") for s in disc_services} # Mappatura nome->ID per eventuale auto-fix
        is_valid, val_errors = PlanValidator.validate(plan, available_ids)
        if not is_valid:
            print(f"[VALIDATION] {len(val_errors)} errore/i:")
            for e in val_errors:
                print(f"  - {e}")
            # Tentativo di riparazione automatica del piano
            plan = self.planner.repair(plan, available_ids, name_to_id)
            is_valid, val_errors = PlanValidator.validate(plan, available_ids)
            if not is_valid:
                # Se non è possibile riparare, annulliamo le attività
                print("[VALIDATION] Piano non recuperabile - esecuzione annullata.")
                plan["tasks"] = []

        validated_tasks_count = (
            len(plan.get("tasks", []))
            if isinstance(plan, dict) and isinstance(plan.get("tasks"), list)
            else 0
        )
        if planner_status == "NONEMPTY" and validated_tasks_count == 0:
            route_reason = "POST_VALIDATION_EMPTY"

        # Dry-run: fermati dopo planning + validazione. Utile per l'harness del
        # Designer, che non deve eseguire piani potenzialmente write-side.
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
        # Eseguiamo il piano e raccogliamo i risultati
        results = self.executor.run(plan, disc_services, self.backend_mode)

        # Se l'executor ritorna un file, lo restituiamo come Response
        if isinstance(results, dict) and results.get("status") == "FILE":
            return Response(results["body"], status=results["status_code"],
                            headers=results["headers"])

        # Pulizia dei file temporanei salvati in `Files`
        if os.path.exists("Files"):
            for filename in os.listdir("Files"):
                file_path = os.path.join("Files", filename)
                try:
                    if os.path.isfile(file_path): os.unlink(file_path)
                    elif os.path.isdir(file_path): shutil.rmtree(file_path)
                except Exception as e:
                    # Non blocchiamo l'esecuzione in caso di errore di pulizia
                    print(f"[WARN] Pulizia fallita: {e}")

        # Risposta finale contenente il piano e i risultati dell'esecuzione
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
