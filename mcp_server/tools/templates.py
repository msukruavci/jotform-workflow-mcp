"""Template search and blueprint recommendation tools for Jotform Workflow MCP."""
from __future__ import annotations

import html
import re
from typing import Annotated, Any
from pydantic import BaseModel, Field
from mcp.server import MCPServer
from mcp_server import audit_log, rag_engine


class TemplateItem(BaseModel):
    id: str = Field(description="Template ID")
    title: str = Field(description="Template title")
    clone_count: int = Field(default=0, description="Usage count")
    steps_summary: list[str] = Field(default_factory=list, description="Step types and names included in the template")
    score: float = Field(description="Relevance score (cosine similarity)")
    elements_count: int = Field(default=0, description="Total number of workflow elements/steps")
    links_count: int = Field(default=0, description="Total number of edge connections between steps")
    suggested_form_fields: list[str] = Field(
        default_factory=list,
        description="Field labels inferred from the template configuration; suggestions, not an authoritative schema.",
    )
    elements: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Template element blueprint details for workflow design inspiration.",
    )
    links: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Template link blueprint details for workflow design inspiration.",
    )


class TemplateSearchResult(BaseModel):
    query: str = Field(description="The search query submitted")
    normalized_query: str = Field(description="English query used for embedding and reranking")
    count: int = Field(description="Number of matching templates returned")
    templates: list[TemplateItem] = Field(description="Top-k matching templates")


class WorkflowTemplateDetail(BaseModel):
    id: str = Field(description="Template ID")
    title: str = Field(description="Template title")
    slug: str = Field(default="", description="URL slug")
    description: str = Field(description="Detailed template description")
    tags: str = Field(default="", description="Tags associated with the template")
    clone_count: int = Field(default=0, description="Usage count")
    steps_summary: list[str] = Field(default_factory=list, description="Summary of steps included")
    elements_count: int = Field(default=0, description="Total number of workflow elements/steps")
    links_count: int = Field(default=0, description="Total number of edge connections between steps")
    elements: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Detailed element configs (type, name, outcomes, email templates, assignees, expirations, etc.)",
    )
    links: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Detailed link connections between elements (fromElement, fromPortName, toElement, toPortName)",
    )



def _short_text(value: Any, *, max_chars: int = 180) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", "", str(value or ""))).strip()
    text = re.sub(r"\s+", " ", text)
    return text[:max_chars].rstrip()


def _recipient_blueprint(value: Any) -> list[str]:
    recipients = value if isinstance(value, list) else [value]
    result: list[str] = []
    seen: set[str] = set()
    for item in recipients:
        if isinstance(item, dict):
            raw = item.get("text") or item.get("value") or item.get("name")
        else:
            raw = item
        clean = _short_text(raw, max_chars=100)
        key = clean.casefold()
        if clean and key not in seen:
            seen.add(key)
            result.append(clean)
    return result


def _first_text(element: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = element.get(key)
        if value not in (None, "", [], {}):
            return _short_text(value)
    return ""


def _sanitize_template_element(element: dict[str, Any]) -> dict[str, Any]:
    """Extract a minimal, lightweight blueprint of a template step.

    Keeps compact architectural fields while dropping HTML/CSS-heavy email bodies,
    UUID noise, and canvas layout values.
    """
    element_id = str(element.get("element_id") or element.get("id") or "")
    step_type = str(element.get("type") or "")
    name = element.get("name") or element.get("header")

    blueprint: dict[str, Any] = {
        "element_id": element_id,
        "type": step_type,
    }
    if name:
        blueprint["name"] = str(name)

    if step_type in {"workflow_send_email", "workflow_reminder_email"}:
        recipients = _recipient_blueprint(element.get("to"))
        if recipients:
            blueprint["to"] = recipients
        subject = _first_text(element, "subject")
        if subject:
            blueprint["subject"] = subject

    if step_type in {"workflow_approval", "workflow_assign_task", "workflow_assign_form"}:
        assignees = _recipient_blueprint(element.get("approver") or element.get("assignee"))
        if assignees:
            blueprint["approver" if step_type == "workflow_approval" else "assignee"] = assignees
        subject = _first_text(
            element,
            "subject",
            "approvalEmail__email__subject",
            "assignTaskEmail__email__subject",
            "assignFormEmail__email__subject",
        )
        if subject:
            blueprint["subject"] = subject
        task_description = _first_text(element, "taskDescription", "description")
        if task_description:
            blueprint["taskDescription"] = task_description

    for key in ("formID", "documentID", "integrationID", "action"):
        value = element.get(key)
        if value not in (None, "", [], {}):
            blueprint[key] = _short_text(value, max_chars=120)

    raw_outcomes = element.get("outcomes") or []
    outcomes: list[str] = []
    for o in raw_outcomes:
        if isinstance(o, dict):
            label = o.get("branchName") or o.get("name") or o.get("text") or o.get("conditionValue")
            if label:
                outcomes.append(str(label))
        elif isinstance(o, str) and o:
            outcomes.append(o)
    if outcomes:
        blueprint["outcomes"] = outcomes

    return blueprint


def _sanitize_template_link(link: dict[str, Any]) -> dict[str, Any]:
    from_elem = link.get("fromElement")
    to_elem = link.get("toElement")
    labels = link.get("labels") or []
    outcome = ""
    if isinstance(labels, list) and labels and isinstance(labels[0], dict):
        outcome = str(labels[0].get("label") or "")
    if not outcome:
        outcome = str(link.get("outcome") or "")

    clean: dict[str, Any] = {
        "from": from_elem,
        "to": to_elem,
    }
    if outcome:
        clean["outcome"] = outcome
    return clean


def _suggested_form_fields(elements: list[dict[str, Any]]) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        clean = html.unescape(re.sub(r"<[^>]+>", "", value)).strip()
        key = clean.casefold()
        if clean and len(clean) <= 100 and key not in seen:
            seen.add(key)
            labels.append(clean)

    def inspect(value) -> None:
        if isinstance(value, dict):
            field = value.get("field")
            if isinstance(field, str) and not field.isdigit():
                add(field)
            for nested in value.values():
                inspect(nested)
        elif isinstance(value, list):
            for nested in value:
                inspect(nested)
        elif isinstance(value, str) and "questionColumn" in value:
            for match in re.findall(r'class="questionColumn"[^>]*>(.*?)</td>', value, flags=re.I | re.S):
                add(match)

    for element in elements:
        inspect(element)
    return labels[:16]


def search_templates_tool(query: str, top_k: int = 1) -> TemplateSearchResult:
    k = max(1, min(top_k, 3))
    results = rag_engine.search_templates(query, top_k=k)
    items = []
    for r in results:
        raw_elements = r.get("elements") or []
        raw_links = r.get("links") or []
        sanitized_elements = [_sanitize_template_element(e) for e in raw_elements if isinstance(e, dict)]
        sanitized_links = [_sanitize_template_link(l) for l in raw_links if isinstance(l, dict)]
        items.append(
            TemplateItem(
                id=str(r.get("id")),
                title=r.get("title", ""),
                clone_count=int(r.get("clone_count") or 0),
                steps_summary=r.get("steps_summary") or [],
                score=float(r.get("score", 0.0)),
                elements_count=len(sanitized_elements),
                links_count=len(sanitized_links),
                suggested_form_fields=_suggested_form_fields(raw_elements),
                elements=sanitized_elements,
                links=sanitized_links,
            )
        )
    return TemplateSearchResult(
        query=query,
        normalized_query=rag_engine.normalize_search_query(query),
        count=len(items),
        templates=items,
    )


def get_template_detail_tool(template_id: str) -> WorkflowTemplateDetail:
    tmpl = rag_engine.get_template_by_id(template_id)
    if not tmpl:
        raise ValueError(f"Workflow template with ID '{template_id}' was not found in catalog.")
    return WorkflowTemplateDetail(
        id=str(tmpl.get("id")),
        title=str(tmpl.get("title", "")),
        slug=str(tmpl.get("slug", "")),
        description=str(tmpl.get("description", "")),
        tags=str(tmpl.get("tags", "")),
        clone_count=int(tmpl.get("clone_count") or 0),
        steps_summary=tmpl.get("steps_summary") or [],
        elements_count=int(tmpl.get("elements_count") or len(tmpl.get("elements") or [])),
        links_count=int(tmpl.get("links_count") or len(tmpl.get("links") or [])),
        elements=tmpl.get("elements") or [],
        links=tmpl.get("links") or [],
    )


for _traced_helper_name in (
    "_sanitize_template_element",
    "_sanitize_template_link",
    "_suggested_form_fields",
    "search_templates_tool",
    "get_template_detail_tool",
):
    globals()[_traced_helper_name] = audit_log.trace_function(globals()[_traced_helper_name])


def register(mcp: MCPServer) -> None:
    @mcp.tool()
    def search_workflow_templates(
        query: Annotated[
            str,
            Field(
                description=(
                    "A concise English query describing the desired workflow idea, domain, or process. "
                    "Translate the user's request to English before calling this tool "
                    "(e.g. 'employee vacation approval flow', 'purchase order approval with signature')."
                )
            ),
        ],
        top_k: Annotated[
            int,
            Field(description="Number of relevant template blueprints to return (default 1, max 3; use 2 only if ambiguous)"),
        ] = 1,
    ) -> TemplateSearchResult:
        """
        Always search the local template catalog first for a close blueprint when building a new workflow.

        Use a concise English query. A result includes compact graph structure and
        inferred suggested_form_fields. Treat low/no matches as no template and continue;
        never force an unrelated blueprint onto the user's request.
        """
        return search_templates_tool(query, top_k)
