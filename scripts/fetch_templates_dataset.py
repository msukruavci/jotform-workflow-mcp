"""
Fetches all Jotform workflow templates across languages and categories,
parses their full approval snapshots (elements + links), calculates complexity
and uniqueness metrics, saves the comprehensive dataset to mcp_server/assets/templates_dataset.json,
and builds a precomputed FAISS vector index (templates.index) for zero-latency RAG search.
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


def infer_category(title: str, tags: str, description: str) -> str:
    text = f"{title} {tags} {description}".lower()
    if any(k in text for k in ["employee", "onboarding", "leave", "time off", "vacation", "hr", "human resource", "hiring", "interview", "recruitment", "resignation", "izin", "personel"]):
        return "Human Resources"
    if any(k in text for k in ["expense", "reimbursement", "budget", "finance", "payment", "purchase", "procurement", "invoice", "harcama", "masraf", "odeme", "fatura"]):
        return "Finance & Procurement"
    if any(k in text for k in ["it", "software", "hardware", "access", "ticket", "equipment", "bug", "support ticket", "teknik", "ekipman"]):
        return "IT & Operations"
    if any(k in text for k in ["customer", "client", "complaint", "feedback", "support", "refund", "return", "destek", "sikayet", "iade"]):
        return "Customer Service"
    if any(k in text for k in ["student", "school", "course", "teacher", "academic", "university", "admission", "ogrenci", "okul"]):
        return "Education"
    if any(k in text for k in ["patient", "medical", "health", "doctor", "hospital", "clinic", "hasta", "saglik"]):
        return "Healthcare"
    if any(k in text for k in ["contract", "legal", "compliance", "agreement", "nda", "sozlesme", "hukuk"]):
        return "Legal & Compliance"
    if any(k in text for k in ["lead", "sales", "discount", "quote", "deal", "crm", "satis"]):
        return "Sales & Marketing"
    return "General Management"


def fetch_template_list(start: int = 0, rpp: int = 50, language: str = "", sorting: str = "popular") -> list[dict]:
    params = urllib.parse.urlencode({
        "rpp": rpp,
        "sorting": sorting,
        "filterListing": "all",
        "start": start,
        "filterStatus": "public",
        "noESign": 0,
        "language": language,
    })
    req = urllib.request.Request(f"{BFF_FILTER_URL}?{params}", headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
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
        LOGGER.warning("Error fetching template list language='%s' start=%d sorting='%s': %s", language, start, sorting, e)
    return []


def fetch_template_detail(template_id: str) -> dict | None:
    req = urllib.request.Request(f"{PUBLIC_API_URL}?id={template_id}", headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("responseCode") == 200:
                return data.get("content")
    except Exception as e:
        LOGGER.warning("Error fetching template detail for id=%s: %s", template_id, e)
    return None


def parse_snapshot(snapshot_raw: str | dict | None) -> tuple[list[dict], list[dict], list[str], dict[str, int]]:
    if not snapshot_raw:
        return [], [], [], {}
    try:
        snapshot = json.loads(snapshot_raw) if isinstance(snapshot_raw, str) else snapshot_raw
        elements = snapshot.get("elements", [])
        links = snapshot.get("links", [])
        steps_summary = []
        step_counts: dict[str, int] = {}
        for el in elements:
            step_type = el.get("type", "unknown")
            name = el.get("name") or el.get("title") or ""
            steps_summary.append(f"{step_type} ({name})".strip())
            step_counts[step_type] = step_counts.get(step_type, 0) + 1
        return elements, links, steps_summary, step_counts
    except Exception:
        return [], [], [], {}


def compute_metrics_and_embeddings(dataset: list[dict]) -> None:
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

    # Compute pairwise similarity matrix to assess uniqueness
    LOGGER.info("Computing pairwise cosine similarity & uniqueness scores...")
    similarity_matrix = np.dot(normalized_matrix, normalized_matrix.T)
    np.fill_diagonal(similarity_matrix, 0.0) # Ignore self-similarity

    for idx, item in enumerate(dataset):
        max_sim = float(np.max(similarity_matrix[idx])) if len(dataset) > 1 else 0.0
        # Uniqueness score: 1.0 = completely unique, 0.0 = exact duplicate
        uniqueness = round(max(0.0, min(1.0, 1.0 - max_sim)), 3)
        
        # Complexity score based on elements, links, and branching
        elem_cnt = item.get("elements_count", 0)
        link_cnt = item.get("links_count", 0)
        has_conditions = 1 if "workflow_conditional_branch" in str(item.get("step_counts", {})) or "workflow_binary_decision" in str(item.get("step_counts", {})) else 0
        complexity = round(float(elem_cnt * 1.0 + link_cnt * 0.8 + has_conditions * 2.0), 1)

        item["uniqueness_score"] = uniqueness
        item["complexity_score"] = complexity

    # Build and write FAISS index
    dimension = normalized_matrix.shape[1]
    index = faiss.IndexFlatIP(dimension)
    index.add(normalized_matrix)

    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    faiss.write_index(index, str(INDEX_PATH))
    LOGGER.info("FAISS index saved to %s (dimension=%d, total=%d)", INDEX_PATH, dimension, index.ntotal)


def main() -> None:
    LOGGER.info("Starting global template dataset collection (across all languages and sorts)...")
    templates_by_id: dict[str, dict] = {}

    # 1. Broad global scan (all languages)
    for start in range(0, 850, 50):
        LOGGER.info("Fetching global template listing page start=%d...", start)
        items = fetch_template_list(start=start, rpp=50, language="", sorting="popular")
        if not items:
            break
        for item in items:
            tid = str(item.get("id"))
            if tid and tid not in templates_by_id:
                templates_by_id[tid] = item

    # 2. Multilingual scans to ensure non-English unique templates are included
    for lang in ["en", "tr", "de", "fr", "it", "es", "pt"]:
        for start in range(0, 300, 50):
            items = fetch_template_list(start=start, rpp=50, language=lang, sorting="popular")
            if not items:
                break
            for item in items:
                tid = str(item.get("id"))
                if tid and tid not in templates_by_id:
                    templates_by_id[tid] = item

    LOGGER.info("Collected %d unique template IDs. Fetching full snapshots concurrently...", len(templates_by_id))

    details_map: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=16) as executor:
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
        tags = item.get("tags") or (detail.get("tags") if detail else "")

        snapshot = detail.get("approval_snapshot") if detail else item.get("approval_snapshot")
        elements, links, steps_summary, step_counts = parse_snapshot(snapshot)
        category = infer_category(title, tags, plain_desc or meta_desc)

        entry = {
            "id": tid,
            "title": title,
            "slug": item.get("slug") or (detail.get("slug") if detail else ""),
            "category": category,
            "description": plain_desc or meta_desc,
            "meta_description": meta_desc,
            "tags": tags,
            "clone_count": int(item.get("clonecount") or (detail.get("clonecount") if detail else 0) or 0),
            "steps_summary": steps_summary,
            "step_counts": step_counts,
            "elements_count": len(elements),
            "links_count": len(links),
            "elements": elements,
            "links": links,
            "search_text": f"[{category}] {title}. {plain_desc or meta_desc}. Steps: {', '.join(steps_summary)}".strip(),
        }
        dataset.append(entry)

    LOGGER.info("Processing metrics and building FAISS index for %d templates...", len(dataset))
    compute_metrics_and_embeddings(dataset)

    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    with open(DATASET_PATH, "w", encoding="utf-8") as f:
        json.dump(dataset, f, ensure_ascii=False, indent=2)

    LOGGER.info("All done! Saved %d templates to %s with full metrics and FAISS index at %s", len(dataset), DATASET_PATH, INDEX_PATH)


if __name__ == "__main__":
    main()
