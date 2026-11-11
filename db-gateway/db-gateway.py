from flask import Flask, request, jsonify
from pymongo import MongoClient
from qdrant_client import QdrantClient
from qdrant_client.models import (
    VectorParams, Distance, PointStruct,
    Filter, FieldCondition, Range, IsEmptyCondition, PayloadField, MatchValue,
)
from bson.json_util import dumps
from cheroot.wsgi import Server as WSGIServer
import uuid
import os
import sys
import logging
import json
import signal
import re
import time
import requests
from sentence_transformers import SentenceTransformer, CrossEncoder
from optimum.onnxruntime import ORTModelForFeatureExtraction, ORTModelForSequenceClassification
from transformers import AutoTokenizer


def handle_sigterm(*args):
    sys.exit(0)

signal.signal(signal.SIGTERM, handle_sigterm)

log_file_path = "test.txt"
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(log_file_path, mode='a', encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger("app")

app = Flask(__name__)

MONGO_USER = os.environ.get("MONGO_USER", "admin")
MONGO_PASS = os.environ.get("MONGO_PASS", "admin")
MONGO_HOST = os.environ.get("MONGO_HOST", "catalog-data")
MONGO_PORT = os.environ.get("MONGO_PORT", "27017")
MONGO_DB   = os.environ.get("MONGO_DB", "microcks")
MONGO_URI  = f"mongodb://{MONGO_USER}:{MONGO_PASS}@{MONGO_HOST}:{MONGO_PORT}/"

QDRANT_HOST             = os.environ.get("QDRANT_HOST", "catalog-vector")
QDRANT_PORT             = os.environ.get("QDRANT_PORT", "6333")
QDRANT_COLLECTION       = os.environ.get("QDRANT_COLLECTION", "services")
QDRANT_COLLECTION_INDEX = os.environ.get("QDRANT_COLLECTION_INDEX", "services_index")
QDRANT_URI              = f"http://{QDRANT_HOST}:{QDRANT_PORT}"

# ── LLM query decomposition (optional) ──────────────────────────────────────
# If USE_LLM_DECOMPOSITION=true, before Stage 1 the query is decomposed
# into atomic sub-queries through an LLM (default glm-4.7-flash via Ollama).
# Each sub-query produces its own top-K set; the deduplicated union is the
# new input to Stage 2. On error it falls back to the original query
# as a single sub-query — the system never breaks.
USE_LLM_DECOMPOSITION  = os.environ.get("USE_LLM_DECOMPOSITION", "true").lower() == "true"
OLLAMA_URL             = os.environ.get("OLLAMA_URL", "http://ollama:11434")
DECOMPOSITION_MODEL    = os.environ.get("DECOMPOSITION_MODEL", "glm-4.7-flash:q4_K_M")
DECOMPOSITION_TIMEOUT  = int(os.environ.get("DECOMPOSITION_TIMEOUT", "15"))
DECOMPOSITION_MAX_SUBQ = int(os.environ.get("DECOMPOSITION_MAX_SUBQ", "4"))

mongo_client = MongoClient(MONGO_URI)
qdrant_client = QdrantClient(QDRANT_URI)

db         = mongo_client[MONGO_DB]
collection = db["services"]

is_server_ready = False
embedding_model = None
reranker_model  = None
tokenizer       = None


class ONNXCrossEncoder:
    """
    Custom wrapper to run a CrossEncoder in ONNX format.
    Massively optimizes CPU inference compared to standard PyTorch.
    """
    def __init__(self, model_name):
        logger.info(f"Converting and loading {model_name} in ONNX format (this may take a minute)...")
        self.tokenizer = AutoTokenizer.from_pretrained(model_name)

        # ── Configurable CPU parallelism (to speed up reranking in tests) ──
        # RERANK_THREADS=0 (default) lets ONNX Runtime decide (usually all
        # cores). Setting it (e.g. to the number of physical cores) controls how
        # many threads inference uses. It does not change results, only speed.
        rerank_threads = int(os.environ.get("RERANK_THREADS", "0"))
        self.batch_size = int(os.environ.get("RERANK_BATCH_SIZE", "4"))

        from optimum.onnxruntime import ORTModelForSequenceClassification as _ORT
        kwargs = dict(export=True, provider="CPUExecutionProvider")
        if rerank_threads > 0:
            import onnxruntime as ort
            so = ort.SessionOptions()
            so.intra_op_num_threads = rerank_threads
            so.inter_op_num_threads = max(1, rerank_threads // 2)
            so.execution_mode = ort.ExecutionMode.ORT_PARALLEL
            kwargs["session_options"] = so
            logger.info(f"ONNX reranker: intra_op={rerank_threads} threads, "
                        f"batch_size={self.batch_size}")

        # The export=True parameter downloads the PyTorch model and converts it to ONNX on the fly
        self.model = _ORT.from_pretrained(model_name, **kwargs)
        logger.info(f"{model_name} successfully loaded in ONNX.")

    def predict(self, sentences, batch_size=None):
        if batch_size is None:
            batch_size = self.batch_size
        all_scores = []
        for i in range(0, len(sentences), batch_size):
            batch = sentences[i:i+batch_size]
            # max_length=512 prevents crashes on endpoints with anomalous descriptions
            inputs = self.tokenizer(batch, padding=True, truncation=True, max_length=512, return_tensors="pt")
            
            # Inference through the ONNX runtime (C++)
            outputs = self.model(**inputs)
            logits = outputs.logits
            if logits.ndim > 1:
                logits = logits.squeeze(-1)
            logits = logits.detach().numpy()
            
            # Handling of results (single or multiple)
            if logits.ndim == 0:
                all_scores.append(float(logits))
            else:
                all_scores.extend([float(x) for x in logits])
                
        return all_scores


def load_model():
    global embedding_model, reranker_model, tokenizer
    logger.info("Loading models...")
    
    # 1. Load Embedding Model (kept in standard PyTorch)
    embedding_model = SentenceTransformer(
        model_name_or_path='Qwen/Qwen3-Embedding-0.6B',
        device='cpu',
        trust_remote_code=True
    )
    tokenizer = embedding_model.tokenizer
    logger.info("Embedding model loaded.")
    
    # 2. Load Reranker Model (using the new ONNX engine)
    reranker_model = ONNXCrossEncoder('BAAI/bge-reranker-base')
    logger.info("Reranker model loaded.")


def embed_query(text: str) -> list:
    """
    Embedding for the user query at runtime.
    Prefix 'query:' — Qwen3-Embedding is trained with query/passage asymmetry:
    using the same prefix for queries and documents degrades retrieval.
    """
    embedding = embedding_model.encode(
        f"query: {text}", convert_to_tensor=False, normalize_embeddings=True
    )
    return embedding.tolist()


def embed_passage(text: str) -> list:
    """
    Embedding for documents to index (capabilities, descriptions).
    Prefix 'passage:' — matches the role of documents in Qwen3-Embedding
    training. To be used in all indexing phases.
    """
    embedding = embedding_model.encode(
        f"passage: {text}", convert_to_tensor=False, normalize_embeddings=True
    )
    return embedding.tolist()


def count_tokens(text):
    tokens = tokenizer.encode(text, add_special_tokens=True)
    return len(tokens)


def select_stage1_text(doc: dict) -> tuple[str, str]:
    """
    Selects the text for the Stage 1 index with the following priority:
      1) description + generated_description concatenated (if both present)
      2) description alone (if at least 100 characters long)
      3) generated_description alone (fallback)
      4) short description alone (last fallback)
    """
    description = (doc.get("description") or "").strip()
    generated   = (doc.get("generated_description") or "").strip()

    if description and generated:
        return f"{description} {generated}", "description+generated"
    if len(description) >= 100:
        return description, "description"
    if generated:
        return generated, "generated_description"
    if description:
        return description, "description"
    return "", "none"


# ─────────────────────────────────────────────────────────────────────────────
# ENRICHED RERANK TEXT
#
# The CrossEncoder has poor lexical overlap between the user query and the capability text
# because capabilities describe REST behaviors, not domain concepts.
# Enriching the text with parameters and response schema fields exposes
# domain terms (e.g. "temperature", "Celsius", "sensorType") that the
# CrossEncoder can compare directly with the query.
#
# The enriched text is used ONLY for reranking — it is not stored in
# Qdrant nor sent to the LLM. The LLM context always receives the
# original capability (more readable and concise).
# ─────────────────────────────────────────────────────────────────────────────

def _build_enriched_text(
    http_op: str,
    capabilities: dict,
    parameters: dict,
    response_schemas: dict,
    request_schemas: dict,
) -> str:
    """
    Builds the enriched text for CrossEncoder reranking by combining:
      - capability text (functional description of the endpoint)
      - parameters (names and enum values of query params)
      - response schema fields (names of returned fields)
      - request schema fields (names of input fields, for POST/PUT)
    """
    parts = [capabilities.get(http_op, "")]

    param_str = parameters.get(http_op, "") or ""
    if param_str:
        parts.append(f"| parameters: {param_str}")

    resp_str = response_schemas.get(http_op, "") or ""
    if resp_str:
        fields = re.findall(r'(\w+):', resp_str)
        if fields:
            parts.append(f"| response fields: {', '.join(fields)}")

    req_str = request_schemas.get(http_op, "") or ""
    if req_str and not http_op.startswith("GET"):
        fields = re.findall(r'(\w+):', req_str)
        if fields:
            parts.append(f"| request fields: {', '.join(fields)}")

    return " ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# QUERY DECOMPOSITION (Stage 1 recall enhancer)
#
# Splits a composite user query into atomic sub-queries, each targeting
# a single type of information/service. Improves Stage 1 recall
# on large and cross-domain catalogs where a single embedding of the
# composite query is a semantic compromise that penalizes "marginal" services.
#
# Decomposition is DOMAIN-AGNOSTIC: the prompt contains no catalog-specific
# knowledge, the LLM reasons about the logical form of the query.
#
# On error (toggle off, timeout, malformed JSON, empty list) it
# falls back to the original query as a single sub-query — behavior
# identical to the pre-decomposition pipeline, zero risk of regression.
# ─────────────────────────────────────────────────────────────────────────────

DECOMPOSITION_SYSTEM_PROMPT = """You are a query decomposition assistant for a service discovery system.
Given a user query, decompose it into atomic information needs.
Each sub-query targets ONE type of information that can be satisfied by a single type of API/service.

RULES:
- Output 1 to 4 sub-queries.
- If the query asks for ONE type of information, output exactly one sub-query.
- Each sub-query is a short noun phrase (2-6 words), describing the resource/data type.
- Strip references to specific entities, locations, filters, conditions — keep only the resource type.
- Sub-queries must be independent (no references between them).
- Use the same language as the input query.
- Do NOT invent information not implied by the query.

EXAMPLES:
Input: "list all available double rooms"
Output: {"sub_queries": ["room availability", "room types"]}

Input: "find family suites near the wellness center with sea view"
Output: {"sub_queries": ["room inventory", "hotel amenities", "room features"]}

Input: "show me average room prices during the high season holidays"
Output: {"sub_queries": ["pricing rates", "seasonal calendars"]}

Input: "book a massage session for tomorrow afternoon"
Output: {"sub_queries": ["spa reservations", "guest bookings"]}

Input: "show me guest reviews for rooms on the top floor"
Output: {"sub_queries": ["guest reviews", "room locations"]}
"""

DECOMPOSITION_SCHEMA = {
    "type": "object",
    "properties": {
        "sub_queries": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": DECOMPOSITION_MAX_SUBQ,
        }
    },
    "required": ["sub_queries"],
}


def decompose_query(query_text: str) -> tuple[list, dict]:
    """
    Decomposes the query into atomic sub-queries through an LLM.

    Returns:
        (sub_queries, meta) where meta contains:
          - source: "llm" | "fallback" | "disabled"
          - latency_ms: int
          - reason: fallback reason (if applicable)
    """
    if not USE_LLM_DECOMPOSITION:
        return [query_text], {"source": "disabled", "latency_ms": 0, "reason": None}

    payload = {
        "model": DECOMPOSITION_MODEL,
        "messages": [
            {"role": "system", "content": DECOMPOSITION_SYSTEM_PROMPT},
            {"role": "user",   "content": query_text},
        ],
        "format": DECOMPOSITION_SCHEMA,
        "options": {
            "temperature": 0.0,
            "num_predict": 200,
        },
        "think":  False,
        "stream": False,
    }

    t0 = time.perf_counter()
    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json=payload,
            timeout=DECOMPOSITION_TIMEOUT,
        )
        resp.raise_for_status()
        latency_ms = int((time.perf_counter() - t0) * 1000)

        content = resp.json().get("message", {}).get("content", "")
        parsed  = json.loads(content)
        sub_queries = parsed.get("sub_queries", [])

        # Sanitize: strip, dedupe (case-insensitive), drop empty, cap to MAX_SUBQ
        seen, clean = set(), []
        for sq in sub_queries:
            sq = (sq or "").strip()
            if not sq or sq.lower() in seen:
                continue
            seen.add(sq.lower())
            clean.append(sq)
        clean = clean[:DECOMPOSITION_MAX_SUBQ]

        if not clean:
            return [query_text], {"source": "fallback",
                                  "latency_ms": latency_ms,
                                  "reason": "empty_sub_queries"}

        return clean, {"source": "llm", "latency_ms": latency_ms, "reason": None}

    except (requests.exceptions.RequestException,
            json.JSONDecodeError, ValueError, KeyError) as e:
        return [query_text], {"source": "fallback",
                              "latency_ms": int((time.perf_counter() - t0) * 1000),
                              "reason": type(e).__name__}


def create_vector_collection():
    existing_collections = {col.name for col in qdrant_client.get_collections().collections}

    if QDRANT_COLLECTION not in existing_collections:
        qdrant_client.create_collection(
            collection_name=QDRANT_COLLECTION,
            vectors_config=VectorParams(size=1024, distance=Distance.COSINE)
        )
        logger.info(f"Collection '{QDRANT_COLLECTION}' created")
    else:
        logger.info(f"Collection '{QDRANT_COLLECTION}' already exists")

    if QDRANT_COLLECTION_INDEX not in existing_collections:
        qdrant_client.create_collection(
            collection_name=QDRANT_COLLECTION_INDEX,
            vectors_config=VectorParams(size=1024, distance=Distance.COSINE)
        )
        logger.info(f"Collection '{QDRANT_COLLECTION_INDEX}' created")
    else:
        logger.info(f"Collection '{QDRANT_COLLECTION_INDEX}' already exists")


@app.route("/health")
def index():
    if is_server_ready is True:
        return jsonify({"status": "ok", "message": "Gateway Server is ready", "model_loaded": True}), 200
    else:
        logger.error("Model not yet loaded or broken")
        return jsonify({"status": "error", "message": "Model not yet loaded or broken", "model_loaded": False}), 500


@app.route("/index/search", methods=["POST"])
def vector_search():
    """
    Two-stage retrieval pipeline with optional decomposition and RRF ranking:

    Stage 0 (optional) — Query decomposition via LLM:
      If USE_LLM_DECOMPOSITION=true, the query is split into atomic
      sub-queries. Each sub-query produces its own set of candidates in
      Stage 1; the deduplicated union (max-score) becomes the input to Stage 2.
      The ORIGINAL query is still passed to the CrossEncoder to preserve
      the operational context (entities, filters, conditions).

    Stage 1 — Bi-encoder on descriptions (services_index):
      Retrieves the top-K services by semantic similarity on the description.

    Stage 2 — Cross-encoder on capabilities (reranker BAAI/bge-reranker-base):
      For each service retrieved in Stage 1, loads the endpoints from MongoDB
      and reranks them with the CrossEncoder using the original query.

    Final ranking — Reciprocal Rank Fusion (RRF):
      Combines the Stage 1 (s1_score) and Stage 2 (best_ep_score) rankings
      by summing 1/(K + rank_i) for each ranking. Standard IR technique
      (Cormack, Clarke, Buettcher 2009) that avoids normalizing
      scores on incompatible scales (cosine similarity vs logit). No
      empirical hyperparameter: K=60 is the canonical value in the literature.

    Token budget with graceful trimming:
      Instead of discarding an entire service when it does not fit the budget,
      progressively scales down the number of endpoints keeping the top-N
      by ep_score until it finds the minimal configuration that fits.
    """
    data = request.get_json()
    if not data or "query" not in data:
        return jsonify({"error": "Missing 'query' field"}), 400

    query_text = data["query"]

    # ── Catalog scoping for the robustness test (optional) ─────────────
    # max_rank: includes in Stage 1 only services WITHOUT sample_rank (the
    # smart-city mocks, always present) PLUS distractors with sample_rank < max_rank.
    # Allows simulating a "catalog with N distractors" without importing/deleting them
    # physically. Absent or null = no filter (production behavior).
    max_rank = data.get("max_rank")
    stage1_filter = None
    if max_rank is not None:
        stage1_filter = Filter(should=[
            IsEmptyCondition(is_empty=PayloadField(key="sample_rank")),
            FieldCondition(key="sample_rank", range=Range(lt=int(max_rank))),
        ])
        logger.info(f"[SCOPE] max_rank={max_rank}: distractors with rank<{max_rank} + smart-city")

    # ── Pipeline parameters ────────────────────────────────────────────
    STAGE1_K                  = 7
    # 6 = maximum number of endpoints per service in the smart-city catalog
    # (GET list, GET/PUT/DELETE /{id}, POST, PUT /zone/...). Keeping them all
    # avoids the reranker discarding the GET-list in read queries or
    # the write endpoint in find-then-act queries, without resorting to a
    # query-agnostic heuristic boost. The token budget (5000) acts as a guard.
    TOP_ENDPOINTS_PER_SERVICE = 6
    K_RRF                     = 60   # canonical parameter (Cormack et al., 2009)

    # ════════════════════════════════════════════════════════════════════════
    # STAGE 0: QUERY DECOMPOSITION (optional)
    # ════════════════════════════════════════════════════════════════════════
    logger.info(f"[QUERY] {query_text}")
    sub_queries, decomp_meta = decompose_query(query_text)
    logger.info(
        f"[DECOMPOSITION] source={decomp_meta['source']} "
        f"latency={decomp_meta['latency_ms']}ms "
        f"reason={decomp_meta['reason']} | "
        f"{len(sub_queries)} sub-queries: {sub_queries}"
    )

    # ════════════════════════════════════════════════════════════════════════
    # STAGE 1: union of the top-K for each sub-query (max-score per service)
    # ════════════════════════════════════════════════════════════════════════
    stage1_scores: dict = {}
    per_subquery_log = []

    for sq in sub_queries:
        sq_embedding = embed_query(sq)
        # query_points is the successor of search() (deprecated in qdrant-client 1.10+).
        # Returns a QueryResponse with .points containing ScoredPoint identical
        # to those returned by search(): same .id, .score, .payload attributes.
        sq_response = qdrant_client.query_points(
            collection_name=QDRANT_COLLECTION_INDEX,
            query=sq_embedding,
            limit=STAGE1_K,
            query_filter=stage1_filter,   # None = no filter (production)
        )
        sq_results = sq_response.points
        ids_for_log = []
        for r in sq_results:
            sid = r.payload["mongo_id"]
            if sid not in stage1_scores or r.score > stage1_scores[sid]:
                stage1_scores[sid] = r.score
            ids_for_log.append(f"{sid}({r.score:.3f})")
        per_subquery_log.append(f"  '{sq}' → {ids_for_log}")

    if not stage1_scores:
        logger.warning("[SEARCH] Stage 1: no service found in services_index")
        return jsonify({"results": []}), 200

    logger.info(
        f"[STAGE 1] union of {len(sub_queries)} sub-queries → "
        f"{len(stage1_scores)} unique services:\n" +
        "\n".join(per_subquery_log) +
        f"\n  UNION: {list(stage1_scores.keys())}"
    )

    # ════════════════════════════════════════════════════════════════════════
    # STAGE 2: endpoint reranking — single-batch for all services
    #
    # We load services from MongoDB with projection (no BSON roundtrip),
    # build all (query, enriched_text) pairs in a single list
    # and make ONE SINGLE call to predict().
    # NOTE: the original query_text is ALWAYS used, not the sub-queries.
    # ════════════════════════════════════════════════════════════════════════
    services_data = {}
    for doc_id in stage1_scores:
        # Projection: exclude _id (we already have it) and select only
        # the necessary fields. No dumps/loads roundtrip, all sub-fields
        # are string dicts (no nested ObjectId).
        retrieved = collection.find_one(
            {"_id": doc_id},
            {"_id": 0, "name": 1, "description": 1, "capabilities": 1,
             "endpoints": 1, "response_schemas": 1, "request_schemas": 1,
             "parameters": 1}
        )
        if not retrieved:
            logger.warning(f"[STAGE 2] Service '{doc_id}' not found in MongoDB")
            continue

        capabilities = retrieved.get("capabilities", {}) or {}
        # Excluded: POST /register (mock internal registration) and any
        # /health endpoint (useful for liveness check, useless for the LLM planner).
        ops = [op for op in capabilities.keys()
               if op != "POST /register" and not op.endswith("/health")]
        if not ops:
            continue

        services_data[doc_id] = {
            "s1_score":         stage1_scores[doc_id],
            "name":             retrieved.get("name"),
            "description":      retrieved.get("description"),
            "capabilities":     capabilities,
            "endpoints":        retrieved.get("endpoints", {}) or {},
            "response_schemas": retrieved.get("response_schemas", {}) or {},
            "request_schemas":  retrieved.get("request_schemas", {}) or {},
            "parameters":       retrieved.get("parameters", {}) or {},
            "ops":              ops,
        }

    if not services_data:
        return jsonify({"results": []}), 200

    # ── Build all pairs in a single list ────────────────────────
    all_pairs = []
    pair_map  = []  # (doc_id, op) for each element of all_pairs

    for doc_id, sdata in services_data.items():
        for op in sdata["ops"]:
            enriched = _build_enriched_text(
                op,
                sdata["capabilities"],
                sdata["parameters"],
                sdata["response_schemas"],
                sdata["request_schemas"],
            )
            all_pairs.append((query_text, enriched))
            pair_map.append((doc_id, op))

    logger.info(f"[STAGE 2] Single-batch reranking: {len(all_pairs)} pairs "
                f"from {len(services_data)} services")

    # ── Single call to predict() for all pairs (with batching) ──
    # We set batch_size=4 to force the CrossEncoder to compute small blocks,
    # preventing RAM exhaustion and Swap Disk usage on CPU in Docker.
    # batch_size=None → uses RERANK_BATCH_SIZE (default 4). Increasing it improves
    # CPU utilization on large catalogs, at the cost of more RAM.
    all_scores = reranker_model.predict(all_pairs)

    # ── Aggregate scores by doc_id ─────────────────────────────────────────
    scores_by_service: dict = {doc_id: {} for doc_id in services_data}
    for (doc_id, op), score in zip(pair_map, all_scores):
        scores_by_service[doc_id][op] = float(score)

    merged: dict = {}
    for doc_id, sdata in services_data.items():
        ops = sdata["ops"]

        # Pure sorting by CrossEncoder score: no heuristic boost.
        # The CrossEncoder knows the specific query and assigns the correct score
        # to each endpoint — overriding it with a query-agnostic boost degrades
        # selection for queries that do not require a GET list.
        scored_ops = sorted(
            [(op, scores_by_service[doc_id][op]) for op in ops],
            key=lambda x: x[1],
            reverse=True
        )

        relevant_ops = scored_ops[:TOP_ENDPOINTS_PER_SERVICE]

        # Real best semantic score — uses max() on the raw scores, NOT relevant_ops[0]
        # which could be inflated by the boost.
        best_endpoint_score = max(scores_by_service[doc_id].values())

        merged[doc_id] = {
            "_id":              doc_id,
            "name":             sdata["name"],
            "description":      sdata["description"],
            "capabilities":     {op: sdata["capabilities"][op] for op, _ in relevant_ops},
            "endpoints":        {op: sdata["endpoints"].get(op) for op, _ in relevant_ops},
            "response_schemas": {op: sdata["response_schemas"].get(op) for op, _ in relevant_ops},
            "request_schemas":  {op: sdata["request_schemas"].get(op) for op, _ in relevant_ops},
            "parameters":       {op: sdata["parameters"].get(op) for op, _ in relevant_ops},
            "_stage1_score":    sdata["s1_score"],
            "_best_ep_score":   best_endpoint_score,
        }

        ep_rows = "\n            ".join(
            f"{op:<40} score={score:.4f}" for op, score in relevant_ops
        )
        discarded_ops = [op for op, _ in scored_ops[TOP_ENDPOINTS_PER_SERVICE:]]
        disc_str = f"\n          DISCARDED : {discarded_ops}" if discarded_ops else ""
        logger.info(
            f"[STAGE 2] {doc_id}\n"
            f"          s1={sdata['s1_score']:.4f} | best_ep={best_endpoint_score:.4f}\n"
            f"          SELECTED ({len(relevant_ops)}/{len(ops)}):\n"
            f"            {ep_rows}"
            f"{disc_str}"
        )

    if not merged:
        return jsonify({"results": []}), 200

    # ════════════════════════════════════════════════════════════════════════
    # RECIPROCAL RANK FUSION (RRF)
    #
    # Combines two service rankings without needing to normalize the scales:
    #   - ranking by s1_score (cosine similarity on the description, range 0-1)
    #   - ranking by best_ep_score (CrossEncoder logit, range about ±10)
    #
    # Formula: rrf(s) = Σ 1/(K + rank_i(s))  for each ranking i
    # K=60 is the canonical value in the literature (Cormack et al. 2009),
    # large enough to smooth the differences of top ranks and
    # small enough not to flatten the whole ranking.
    #
    # Advantages over composite scoring ALPHA*s1 + BETA*sigmoid(z):
    #   - zero empirical hyperparameters to justify
    #   - robust to outliers (a very high best_ep_score does not destroy the ranking)
    #   - solid bibliographic citation for the thesis defense
    # ════════════════════════════════════════════════════════════════════════
    services_list = list(merged.values())

    ranked_by_s1 = sorted(services_list, key=lambda x: x["_stage1_score"],   reverse=True)
    ranked_by_ep = sorted(services_list, key=lambda x: x["_best_ep_score"], reverse=True)

    # 1-based rank as per the original formulation of Cormack et al. (2009):
    # the first classified has rank=1, contributing 1/(K+1) to the score.
    s1_rank = {s["_id"]: i for i, s in enumerate(ranked_by_s1, start=1)}
    ep_rank = {s["_id"]: i for i, s in enumerate(ranked_by_ep, start=1)}

    for s in services_list:
        s["_rrf_score"] = (1.0 / (K_RRF + s1_rank[s["_id"]])
                         + 1.0 / (K_RRF + ep_rank[s["_id"]]))

    ordered_services = sorted(services_list, key=lambda x: x["_rrf_score"], reverse=True)

    rrf_rows = "\n    ".join(
        f"  {s['_id']:<50} s1_rank={s1_rank[s['_id']]:<2} "
        f"ep_rank={ep_rank[s['_id']]:<2} rrf={s['_rrf_score']:.5f}"
        for s in ordered_services
    )
    logger.info(
        f"[RRF] K={K_RRF} | final ranking of {len(ordered_services)} services:\n"
        f"    {rrf_rows}"
    )

    # Cleanup internal fields before the token budget
    for s in ordered_services:
        s.pop("_stage1_score", None)
        s.pop("_best_ep_score", None)
        s.pop("_rrf_score", None)

    # ════════════════════════════════════════════════════════════════════════
    # TOKEN BUDGET with graceful trimming
    #
    # For each service it first attempts insertion with all endpoints.
    # If it does not fit the remaining budget, it progressively scales down the number
    # of endpoints (top-N by ep_score, already sorted) until it finds the
    # minimal configuration that fits. Only if not even the single best
    # endpoint fits the budget is the service discarded.
    # ════════════════════════════════════════════════════════════════════════
    _DICT_FIELDS = ("capabilities", "endpoints", "response_schemas",
                    "request_schemas", "parameters")

    def _trim_service(s: dict, keep_ops: list) -> dict:
        """Returns a copy of the service with only the endpoints in keep_ops."""
        trimmed = {}
        for k, v in s.items():
            if k in _DICT_FIELDS and isinstance(v, dict):
                trimmed[k] = {op: v[op] for op in keep_ops if op in v}
            else:
                trimmed[k] = v
        return trimmed

    # Token budget for the entire retrieval payload (measured with the reranker
    # tokenizer). Feeds the Planner prompt, which runs at num_ctx=16384:
    #Configurable via env to adapt it to the hardware.
    max_tokens     = int(os.environ.get("RETRIEVAL_TOKEN_BUDGET", "7000"))
    current_tokens = 0
    top_results    = []
    budget_log     = []  # (service_id, n_inserted, n_total, endpoint_names)

    for s in ordered_services:
        ops = list(s.get("capabilities", {}).keys())

        inserted = False
        for n in range(len(ops), 0, -1):
            candidate     = _trim_service(s, ops[:n])
            candidate_tok = count_tokens(json.dumps(candidate))

            if current_tokens + candidate_tok <= max_tokens:
                top_results.append(candidate)
                current_tokens += candidate_tok
                budget_log.append((s["_id"], n, len(ops), ops[:n]))
                inserted = True
                break

        if not inserted:
            logger.info(
                f"[BUDGET] {s['_id']} excluded: not even the top-1 endpoint "
                f"({count_tokens(json.dumps(_trim_service(s, ops[:1])))} tok) "
                f"fits in the remaining budget ({max_tokens - current_tokens} tok)"
            )

    budget_rows = "\n    ".join(
        f"  {sid:<50} ({n}/{tot} ep) → {ep_names}"
        for sid, n, tot, ep_names in budget_log
    )
    logger.info(
        f"[SEARCH] Stage1={len(stage1_scores)} → Stage2={len(merged)} → "
        f"RRF_ranked={len(ordered_services)} → "
        f"{len(top_results)} in budget | tokens used: {current_tokens}/{max_tokens}\n"
        f"    {budget_rows}"
    )
    return jsonify({"results": top_results}), 200


@app.route("/service", methods=["POST"])
def create_or_update_service():
    data = request.get_json()
    if not data or "id" not in data:
        return jsonify({"error": "Missing 'id' field"}), 400

    doc_id = data["id"]
    data["_id"] = doc_id
    data.pop("id", None)

    # ── Stage 2 index: 1 vector per endpoint (capability text) ──────────────
    capabilities = data.get("capabilities", {})
    for http_op, capability in capabilities.items():
        embedding = embed_passage(capability)
        vector_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, capability))
        qdrant_client.upsert(
            collection_name=QDRANT_COLLECTION,
            points=[
                PointStruct(
                    id=vector_id,
                    vector=embedding,
                    payload={"mongo_id": doc_id, "http_operation": http_op}
                )
            ]
        )

    # ── Stage 1 index: 1 vector per service (description text) ─────────────
    # Prefers description. Uses generated_description only if description
    # is absent or too short (<100 characters).
    text_to_index, source = select_stage1_text(data)
    if text_to_index:
        desc_vector_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"desc:{doc_id}"))
        desc_embedding = embed_passage(text_to_index)
        # sample_rank (if present in the doc) goes into the Stage 1 payload: it is used by the
        # max_rank filter for catalog scoping in robustness tests. The smart-city
        # mocks do not have this field → they always remain included.
        stage1_payload = {"mongo_id": doc_id}
        if data.get("sample_rank") is not None:
            stage1_payload["sample_rank"] = int(data["sample_rank"])
        qdrant_client.upsert(
            collection_name=QDRANT_COLLECTION_INDEX,
            points=[
                PointStruct(
                    id=desc_vector_id,
                    vector=desc_embedding,
                    payload=stage1_payload
                )
            ]
        )
        logger.info(f"[INDEX] Service '{doc_id}' indexed in services_index (source: {source})")

    collection.replace_one({"_id": doc_id}, data, upsert=True)
    return jsonify({"status": "ok", "id": doc_id}), 200


@app.route("/services/<string:service_id>/rank", methods=["PATCH"])
def set_service_rank(service_id):
    """
    Sets sample_rank on an already existing service, WITHOUT re-embedding:
      - updates the Mongo document;
      - updates the point payload in the Stage 1 index (services_index)
        via set_payload (filter on mongo_id).
    Used by the backfill to tag already loaded distractors, so the
    max_rank filter can scope the catalog without reimporting anything.
    Body: {"sample_rank": <int>}
    """
    data = request.get_json(silent=True) or {}
    if "sample_rank" not in data:
        return jsonify({"error": "Missing 'sample_rank'"}), 400
    rank = int(data["sample_rank"])

    result = collection.update_one({"_id": service_id}, {"$set": {"sample_rank": rank}})
    if result.matched_count == 0:
        return jsonify({"error": f"Service '{service_id}' not found"}), 404

    qdrant_client.set_payload(
        collection_name=QDRANT_COLLECTION_INDEX,
        payload={"sample_rank": rank},
        points=Filter(must=[FieldCondition(key="mongo_id", match=MatchValue(value=service_id))]),
    )
    return jsonify({"status": "ok", "id": service_id, "sample_rank": rank}), 200


@app.route("/services", methods=["GET"])
def list_services():
    docs = list(collection.find())
    return dumps(docs), 200


@app.route("/services/<string:service_id>", methods=["GET"])
def get_service(service_id):
    doc = collection.find_one({"_id": service_id})
    if not doc:
        return jsonify({"error": "Service not found"}), 404
    return dumps(doc), 200


@app.route("/services/<string:service_id>", methods=["DELETE"])
def delete_service(service_id):
    result = collection.delete_one({"_id": service_id})
    if result.deleted_count == 0:
        return jsonify({"error": "Service not found"}), 404
    return jsonify({"status": "deleted", "id": service_id}), 200


@app.route("/index/reindex", methods=["POST"])
def reindex_descriptions():
    """
    Reindexes the descriptive texts of all services in services_index (Stage 1).

    Prefers description; uses generated_description only as a fallback when
    description is absent or shorter than 100 characters.
    Uses embed_passage() for the correct asymmetric role.
    """
    docs    = list(collection.find())
    indexed = 0
    skipped = 0
    for doc in docs:
        doc_id = doc.get("_id")
        text_to_index, source = select_stage1_text(doc)
        if not text_to_index:
            logger.warning(f"[REINDEX] {doc_id}: no description available, skipped")
            skipped += 1
            continue
        try:
            desc_vector_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"desc:{doc_id}"))
            desc_embedding = embed_passage(text_to_index)
            # Copies sample_rank from the Mongo doc into the Stage 1 payload, so
            # distractors remain filterable with max_rank even after reindex.
            reindex_payload = {"mongo_id": doc_id}
            if doc.get("sample_rank") is not None:
                reindex_payload["sample_rank"] = int(doc["sample_rank"])
            qdrant_client.upsert(
                collection_name=QDRANT_COLLECTION_INDEX,
                points=[
                    PointStruct(
                        id=desc_vector_id,
                        vector=desc_embedding,
                        payload=reindex_payload
                    )
                ]
            )
            logger.info(f"[REINDEX] {doc_id} indexed (source: {source})")
            indexed += 1
        except Exception as e:
            logger.error(f"[REINDEX] {doc_id} failed: {e}")
            skipped += 1

    logger.info(f"[REINDEX] Completed: {indexed} indexed, {skipped} skipped")
    return jsonify({"indexed": indexed, "skipped": skipped}), 200


@app.route("/services/<string:service_id>/schemas", methods=["PATCH"])
def update_service_schemas(service_id):
    """
    Updates response_schemas, request_schemas, parameters and/or
    generated_description of a service.
    Called by api-importer after extracting the schemas with prance.
    """
    data = request.get_json()
    update_fields = {}
    if "response_schemas" in data:
        update_fields["response_schemas"] = data["response_schemas"]
    if "request_schemas" in data:
        update_fields["request_schemas"] = data["request_schemas"]
    if "parameters" in data:
        update_fields["parameters"] = data["parameters"]
    if "generated_description" in data:
        update_fields["generated_description"] = data["generated_description"]

    if not update_fields:
        return jsonify({"error": "No valid schema fields provided"}), 400

    result = collection.update_one(
        {"_id": service_id},
        {"$set": update_fields}
    )

    if result.matched_count == 0:
        return jsonify({"error": f"Service '{service_id}' not found"}), 404

    logger.info(f"[SCHEMAS] Updated {list(update_fields.keys())} for {service_id}")
    return jsonify({"status": "ok", "id": service_id}), 200


if __name__ == "__main__":
    try:
        with app.app_context():
            logger.info("🛠️ Creating Qdrant collection...")
            create_vector_collection()
            logger.info("📦 Loading embedding model...")
            load_model()
            is_server_ready = True
            logger.info("✅ Server is ready.")
    except Exception as e:
        logger.exception("❌ Failed to initialize application")
        sys.exit(1)

    server = WSGIServer(('0.0.0.0', 5000), app)
    try:
        print("🚀 Starting Flask app with Cheroot on http://0.0.0.0:5000")
        server.start()
    except KeyboardInterrupt:
        print("🛑 Shutting down server...")
        server.stop()