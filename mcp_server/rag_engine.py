"""
Local RAG Engine for Jotform Workflow Templates.

Uses fastembed (BAAI/bge-small-en-v1.5) for vector embeddings and FAISS (faiss-cpu)
for high-performance in-memory and disk-backed vector similarity search.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
LOGGER = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
ASSETS_DIR = REPO_ROOT / "mcp_server" / "assets"
DATASET_PATH = ASSETS_DIR / "templates_dataset.json"
INDEX_PATH = ASSETS_DIR / "templates.index"

_EMBEDDING_MODEL = None
_DATASET: list[dict[str, Any]] = []
_TEMPLATES_BY_ID: dict[str, dict[str, Any]] = {}
_FAISS_INDEX = None


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
    if not _DATASET:
        if DATASET_PATH.is_file():
            try:
                with open(DATASET_PATH, encoding="utf-8") as f:
                    _DATASET = json.load(f)
                _TEMPLATES_BY_ID = {str(item.get("id")): item for item in _DATASET}
                LOGGER.info("Loaded %d template items from %s", len(_DATASET), DATASET_PATH)
            except Exception as e:
                LOGGER.error("Failed to load dataset from %s: %s", DATASET_PATH, e)
        else:
            LOGGER.warning("Dataset file %s does not exist yet.", DATASET_PATH)
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

    if INDEX_PATH.is_file():
        try:
            _FAISS_INDEX = faiss.read_index(str(INDEX_PATH))
            if _FAISS_INDEX.ntotal == len(dataset):
                LOGGER.info("Loaded pre-computed FAISS index from %s (%d vectors)", INDEX_PATH, _FAISS_INDEX.ntotal)
                return _FAISS_INDEX
        except Exception as e:
            LOGGER.warning("Failed to load FAISS index from %s: %s", INDEX_PATH, e)

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
            faiss.write_index(index, str(INDEX_PATH))
            LOGGER.info("Saved FAISS index to %s", INDEX_PATH)
        except Exception as e:
            LOGGER.warning("Could not write FAISS index to disk: %s", e)
        return _FAISS_INDEX
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

    index = get_faiss_index()
    model = _get_embedding_model()

    if index is None or model is None:
        # Fallback to simple keyword search if vector index is unavailable
        query_lower = query.lower()
        scored = []
        for item in dataset:
            text = (item.get("search_text") or "").lower()
            score = 1.0 if query_lower in text else 0.0
            scored.append((score, item))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [dict(item, score=score) for score, item in scored[:top_k]]

    try:
        query_vector = list(model.embed([query]))[0]
        query_vector = np.array([query_vector], dtype=np.float32)
        norm = np.linalg.norm(query_vector)
        if norm > 0:
            query_vector = query_vector / norm

        k = min(top_k, len(dataset))
        scores, indices = index.search(query_vector, k)

        results = []
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0 or idx >= len(dataset):
                continue
            item = dataset[idx].copy()
            item["score"] = round(float(score), 4)
            results.append(item)

        return results
    except Exception as e:
        LOGGER.error("Error during FAISS template search: %s", e)
        return dataset[:top_k]
