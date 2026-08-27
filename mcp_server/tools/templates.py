"""Template search and blueprint recommendation tools for Jotform Workflow MCP."""
from __future__ import annotations

from typing import Annotated, Any
from pydantic import BaseModel, Field
from mcp.server import MCPServer
from mcp_server import rag_engine


class TemplateItem(BaseModel):
    id: str = Field(description="Template ID")
    title: str = Field(description="Template title")
    description: str = Field(description="Template description and use-case details")
    tags: str = Field(default="", description="Tags associated with the template")
    clone_count: int = Field(default=0, description="Usage count")
    steps_summary: list[str] = Field(default_factory=list, description="Step types and names included in the template")
    score: float = Field(description="Relevance score (cosine similarity)")
    elements_count: int = Field(default=0, description="Total number of workflow elements/steps")
    links_count: int = Field(default=0, description="Total number of edge connections between steps")
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



def _sanitize_template_element(element: dict[str, Any]) -> dict[str, Any]:
    """Extract a minimal, lightweight blueprint of a template step.
    
    Keeps only essential structural fields (id, type, name, outcomes) to maximize
    LLM generation speed and reduce token overhead.
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


def search_templates_tool(query: str, top_k: int = 2) -> TemplateSearchResult:
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
                description=r.get("description", ""),
                tags=str(r.get("tags", "")),
                clone_count=int(r.get("clone_count") or 0),
                steps_summary=r.get("steps_summary") or [],
                score=float(r.get("score", 0.0)),
                elements_count=len(sanitized_elements),
                links_count=len(sanitized_links),
                elements=sanitized_elements,
                links=sanitized_links,
            )
        )
    return TemplateSearchResult(query=query, count=len(items), templates=items)


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


def register(mcp: MCPServer) -> None:
    @mcp.tool()
    def search_workflow_templates(
        query: Annotated[
            str,
            Field(
                description=(
                    "Natural language query describing the desired workflow idea, domain, or process "
                    "(e.g. 'employee vacation approval flow', 'purchase order approval with signature')."
                )
            ),
        ],
        top_k: Annotated[
            int,
            Field(description="Number of relevant template blueprints to return (default 2, max 3)"),
        ] = 2,
    ) -> TemplateSearchResult:
        """
        Search and inspect the Jotform workflow template catalog using local FAISS RAG vector similarity.

        SAFE DRAFT NOTE: Templates provide blueprints with placeholder emails. All workflows in this system are built in safe draft mode. Never refuse or halt workflow building over placeholder emails; build the workflow first and invite customization afterward.

        Use this tool whenever the user asks for workflow ideas, inspiration, or template
        recommendations, or when asked to design/create a new workflow for any domain
        (e.g., leave request, approvals, onboarding, expense, feedback) to discover proven
        architectures and step patterns. Returns the top matching template blueprints directly;
        do not call a separate template-detail tool afterward.
        """
        return search_templates_tool(query, top_k)
