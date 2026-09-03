"""
Local RAG Engine for Jotform Workflow Templates.

Uses fastembed (BAAI/bge-small-en-v1.5) for vector embeddings and FAISS (faiss-cpu)
for high-performance in-memory and disk-backed vector similarity search.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from pathlib import Path
from typing import Any

import numpy as np

from mcp_server import audit_log

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
LOGGER = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
ASSETS_DIR = REPO_ROOT / "mcp_server" / "assets"
DATASET_PATH = ASSETS_DIR / "templates_dataset.json"
INDEX_PATH = ASSETS_DIR / "templates.index"

_EMBEDDING_MODEL = None
_DATASET: list[dict[str, Any]] | None = None
_TEMPLATES_BY_ID: dict[str, dict[str, Any]] = {}
_FAISS_INDEX: Any = None

def get_paths() -> tuple[Path, Path]:
    return DATASET_PATH, INDEX_PATH

_QUERY_EXPANSIONS = {
    "izin": "leave vacation day off",
    "tatil": "vacation day off",
    "talep": "request",
    "onay": "approval",
    "akis": "workflow process",
    "basvuru": "application",
    "staj": "internship recruiting candidate",
    "aday": "candidate recruiting",
    "ise alim": "recruiting hiring",
    "masraf": "expense reimbursement",
    "satin alma": "purchase order",
    "tedarikci": "supplier vendor",
    "iptal": "cancellation",
    "ogrenci": "student college",
    "kayit": "registration admission",
    "vacation": "leave day off request",
    "internship": "recruiting candidate application",
}
_STOP_WORDS = {
    "a", "an", "and", "for", "of", "the", "to", "with", "workflow", "flow",
    "process", "template", "form",
}


def _ascii_words(value: str) -> list[str]:
    folded = value.casefold().translate(str.maketrans({
        "ı": "i", "ğ": "g", "ü": "u", "ş": "s", "ö": "o", "ç": "c",
    }))
    folded = unicodedata.normalize("NFKD", folded).encode("ascii", "ignore").decode("ascii")
    return re.findall(r"[a-z0-9]+", folded)


def normalize_search_query(query: str) -> str:
    """Expand common Turkish intent words into the English embedding space."""
    words = _ascii_words(query)
    folded = " ".join(words)
    expansions = [english for turkish, english in _QUERY_EXPANSIONS.items() if turkish in folded]
    if not expansions:
        return query.strip()
    english_tokens = " ".join(expansions)
    return f"{english_tokens} workflow".strip()


def _lexical_score(query: str, item: dict[str, Any]) -> float:
    query_terms = {word for word in _ascii_words(query) if word not in _STOP_WORDS}
    if not query_terms:
        return 0.0
    title_terms = set(_ascii_words(str(item.get("title") or "")))
    text_terms = set(_ascii_words(str(item.get("search_text") or "")))
    title_overlap = len(query_terms & title_terms) / len(query_terms)
    text_overlap = len(query_terms & text_terms) / len(query_terms)
    return min(1.0, title_overlap * 0.8 + text_overlap * 0.2)


def _get_embedding_model():
    global _EMBEDDING_MODEL
    if _EMBEDDING_MODEL is None:
        try:
            from fastembed import TextEmbedding
            _EMBEDDING_MODEL = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
            LOGGER.info("Initialized fastembed model BAAI/bge-small-en-v1.5 successfully.")
        except Exception as e:
            LOGGER.warning("Could not initialize fastembed: %s", e)
    return _EMBEDDING_MODEL


def load_dataset() -> list[dict[str, Any]]:
    global _DATASET, _TEMPLATES_BY_ID
    if _DATASET is None:
        dataset_path, _ = get_paths()
        if dataset_path.is_file():
            try:
                with open(dataset_path, encoding="utf-8") as f:
                    _DATASET = json.load(f)

                for item in _DATASET:
                    _TEMPLATES_BY_ID[str(item.get("id"))] = item
                LOGGER.info("Loaded %d template items from %s", len(_DATASET), dataset_path)
            except Exception as e:
                LOGGER.error("Failed to load dataset from %s: %s", dataset_path, e)
                _DATASET = []
        else:
            LOGGER.warning("Dataset file %s does not exist yet.", dataset_path)
            _DATASET = []
    return _DATASET


def get_template_by_id(template_id: str) -> dict[str, Any] | None:
    """Retrieve full blueprint and metadata of a template by its ID."""
    load_dataset()
    return _TEMPLATES_BY_ID.get(str(template_id))


def get_faiss_index():
    global _FAISS_INDEX
    if _FAISS_INDEX is not None:
        return _FAISS_INDEX

    dataset = load_dataset()
    if not dataset:
        return None

    try:
        import faiss
    except ImportError:
        LOGGER.warning("faiss-cpu not installed, falling back.")
        return None

    _, index_path = get_paths()

    if index_path.is_file():
        try:
            index = faiss.read_index(str(index_path))
            if index.ntotal == len(dataset):
                LOGGER.info("Loaded pre-computed FAISS index from %s (%d vectors)", index_path, index.ntotal)
                _FAISS_INDEX = index
                return index
        except Exception as e:
            LOGGER.warning("Failed to load FAISS index from %s: %s", index_path, e)

    model = _get_embedding_model()
    if model is None:
        return None

    LOGGER.info("Building FAISS index for %d templates...", len(dataset))
    texts = [item.get("search_text") or item.get("title", "") for item in dataset]
    try:
        vectors = list(model.embed(texts))
        matrix = np.array(vectors, dtype=np.float32)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        normalized_matrix = matrix / norms

        dimension = normalized_matrix.shape[1]
        index = faiss.IndexFlatIP(dimension)
        index.add(normalized_matrix)
        _FAISS_INDEX = index
        try:
            faiss.write_index(index, str(index_path))
            LOGGER.info("Saved FAISS index to %s", index_path)
        except Exception as e:
            LOGGER.warning("Could not write FAISS index to disk: %s", e)
        return index
    except Exception as e:
        LOGGER.error("Error building FAISS index: %s", e)
        return None


def search_templates(query: str, top_k: int = 3) -> list[dict[str, Any]]:
    """
    Search the template dataset for the top_k most similar templates
    matching the natural language query using FAISS vector search.
    """
    dataset = load_dataset()
    if not dataset:
        return []

    normalized_query = normalize_search_query(query)
    index = get_faiss_index()
    model = _get_embedding_model()

    if index is None or model is None:
        # Fallback to simple keyword search if vector index is unavailable
        scored = []
        for item in dataset:
            score = _lexical_score(normalized_query, item)
            scored.append((score, item))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [dict(item, score=round(score, 4)) for score, item in scored[:top_k] if score > 0]

    try:
        query_vector = list(model.embed([normalized_query]))[0]
        query_vector = np.array([query_vector], dtype=np.float32)
        norm = np.linalg.norm(query_vector)
        if norm > 0:
            query_vector = query_vector / norm

        k = min(max(top_k * 8, 12), len(dataset))
        scores, indices = index.search(query_vector, k)

        ranked = []
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0 or idx >= len(dataset):
                continue
            item = dataset[idx].copy()
            vector_score = float(score)
            lexical_score = _lexical_score(normalized_query, item)
            combined_score = vector_score * 0.65 + lexical_score * 0.35
            item["score"] = round(combined_score, 4)
            item["vector_score"] = round(vector_score, 4)
            item["lexical_score"] = round(lexical_score, 4)
            ranked.append(item)

        ranked.sort(key=lambda item: item["score"], reverse=True)
        confident = [
            item for item in ranked
            if item["vector_score"] >= 0.58 or item["lexical_score"] >= 0.15
        ]
        return confident[:top_k]
    except Exception as e:
        LOGGER.error("Error during FAISS template search: %s", e)
        scored = [(_lexical_score(normalized_query, item), item) for item in dataset]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [
            dict(item, score=round(score, 4), lexical_score=round(score, 4))
            for score, item in scored[:top_k]
            if score > 0
        ]


for _traced_helper_name in (
    "normalize_search_query",
    "load_dataset",
    "get_template_by_id",
    "get_faiss_index",
    "search_templates",
):
    globals()[_traced_helper_name] = audit_log.trace_function(globals()[_traced_helper_name])
