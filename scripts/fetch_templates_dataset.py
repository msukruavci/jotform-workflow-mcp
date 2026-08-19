"""
Fetches Jotform workflow templates, parses their full approval snapshots (elements + links),
cleans their metadata, saves rich dataset to mcp_server/assets/templates_dataset.json,
and builds precomputed FAISS vector index (templates.index) for instant zero-latency RAG search.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import logging
from pathlib import Path
import re
import urllib.parse
import urllib.request
import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
LOGGER = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
ASSETS_DIR = REPO_ROOT / "mcp_server" / "assets"
DATASET_PATH = ASSETS_DIR / "templates_dataset.json"
INDEX_PATH = ASSETS_DIR / "templates.index"

BFF_FILTER_URL = "https://www.jotform.com/API/approval-templates/filter"
PUBLIC_API_URL = "https://api.jotform.com/approval-templates"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.jotform.com/approval-templates/",
}


def clean_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def fetch_template_list(start: int = 0, rpp: int = 50) -> list[dict]:
    params = urllib.parse.urlencode({
        "rpp": rpp,
        "sorting": "popular",
        "filterListing": "all",
        "start": start,
        "filterStatus": "public",
        "noESign": 0,
        "language": "en",
    })
    req = urllib.request.Request(f"{BFF_FILTER_URL}?{params}", headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            content = data.get("content", {})
            if isinstance(content, dict):
                templates = content.get("templates", [])
                if isinstance(templates, list):
                    return templates
                return list(content.values())
            elif isinstance(content, list):
                return content
    except Exception as e:
        LOGGER.warning("Error fetching template list at start=%d: %s", start, e)
    return []


def fetch_template_detail(template_id: str) -> dict | None:
    req = urllib.request.Request(f"{PUBLIC_API_URL}?id={template_id}", headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("responseCode") == 200:
                return data.get("content")
    except Exception as e:
        LOGGER.warning("Error fetching template detail for id=%s: %s", template_id, e)
    return None


def parse_snapshot(snapshot_raw: str | dict | None) -> tuple[list[dict], list[dict], list[str]]:
    if not snapshot_raw:
        return [], [], []
    try:
        snapshot = json.loads(snapshot_raw) if isinstance(snapshot_raw, str) else snapshot_raw
        elements = snapshot.get("elements", [])
        links = snapshot.get("links", [])
        steps_summary = []
        for el in elements:
            step_type = el.get("type", "unknown")
            name = el.get("name") or el.get("title") or ""
            steps_summary.append(f"{step_type} ({name})".strip())
        return elements, links, steps_summary
    except Exception:
        return [], [], []


def build_faiss_index(dataset: list[dict]) -> None:
    try:
        import faiss
        from fastembed import TextEmbedding
    except ImportError as e:
        LOGGER.error("faiss-cpu or fastembed not installed: %s", e)
        return

    LOGGER.info("Generating embeddings with fastembed for %d templates...", len(dataset))
    model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
    texts = [item.get("search_text") or item.get("title", "") for item in dataset]
    vectors = list(model.embed(texts))
    matrix = np.array(vectors, dtype=np.float32)

    # Normalize vectors for cosine similarity
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    normalized_matrix = matrix / norms

    dimension = normalized_matrix.shape[1]
    index = faiss.IndexFlatIP(dimension)
    index.add(normalized_matrix)

    faiss.write_index(index, str(INDEX_PATH))
    LOGGER.info("FAISS index successfully saved to %s (dimension=%d, total=%d)", INDEX_PATH, dimension, index.ntotal)


def main() -> None:
    LOGGER.info("Starting comprehensive template dataset collection...")
    templates_by_id: dict[str, dict] = {}

    for start in range(0, 300, 50):
        LOGGER.info("Fetching template listing page start=%d...", start)
        items = fetch_template_list(start=start, rpp=50)
        if not items:
            break
        for item in items:
            tid = str(item.get("id"))
            if tid and tid not in templates_by_id:
                templates_by_id[tid] = item

    LOGGER.info("Collected %d unique template listings. Fetching full snapshots concurrently...", len(templates_by_id))
    
    details_map: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(fetch_template_detail, tid): tid for tid in templates_by_id}
        for future in as_completed(futures):
            tid = futures[future]
            try:
                detail = future.result()
                if detail:
                    details_map[tid] = detail
            except Exception as e:
                LOGGER.warning("Failed detail fetch for %s: %s", tid, e)

    dataset = []
    for tid, item in templates_by_id.items():
        detail = details_map.get(tid)
        title = item.get("title") or (detail.get("title") if detail else "")
        description_raw = item.get("description") or (detail.get("description") if detail else "")
        plain_desc = clean_html(description_raw)
        meta_desc = item.get("metaDescription") or (detail.get("metaDescription") if detail else "")
        
        snapshot = detail.get("approval_snapshot") if detail else item.get("approval_snapshot")
        elements, links, steps_summary = parse_snapshot(snapshot)

        entry = {
            "id": tid,
            "title": title,
            "slug": item.get("slug") or (detail.get("slug") if detail else ""),
            "description": plain_desc or meta_desc,
            "meta_description": meta_desc,
            "tags": item.get("tags") or "",
            "clone_count": int(item.get("clonecount") or (detail.get("clonecount") if detail else 0) or 0),
            "steps_summary": steps_summary,
            "elements_count": len(elements),
            "links_count": len(links),
            "elements": elements,
            "links": links,
            "search_text": f"{title}. {plain_desc or meta_desc}. Steps: {', '.join(steps_summary)}".strip(),
        }
        dataset.append(entry)

    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    with open(DATASET_PATH, "w", encoding="utf-8") as f:
        json.dump(dataset, f, ensure_ascii=False, indent=2)

    LOGGER.info("Dataset saved successfully to %s (%d templates with full elements & links)", DATASET_PATH, len(dataset))
    
    # Build FAISS index
    build_faiss_index(dataset)


if __name__ == "__main__":
    main()
