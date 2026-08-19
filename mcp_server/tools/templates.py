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


def search_templates_tool(query: str, top_k: int = 3) -> TemplateSearchResult:
    k = max(1, min(top_k, 10))
    results = rag_engine.search_templates(query, top_k=k)
    items = [
        TemplateItem(
            id=str(r.get("id")),
            title=r.get("title", ""),
            description=r.get("description", ""),
            tags=str(r.get("tags", "")),
            clone_count=int(r.get("clone_count") or 0),
            steps_summary=r.get("steps_summary") or [],
            score=float(r.get("score", 0.0)),
        )
        for r in results
    ]
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
            Field(description="Number of relevant template references to return (default 3, max 10)"),
        ] = 3,
    ) -> TemplateSearchResult:
        """
        Search the Jotform workflow template catalog using local FAISS RAG vector similarity.

        Use this tool whenever the user asks for workflow ideas, inspiration, or template
        recommendations, or when asked to design/create a new workflow for any domain
        (e.g., leave request, approvals, onboarding, expense, feedback) to discover proven
        architectures and step patterns.
        """
        return search_templates_tool(query, top_k)

    @mcp.tool()
    def get_workflow_template(
        template_id: Annotated[
            str,
            Field(
                description="The unique ID of the template to inspect in detail (e.g. '242943285550056')."
            ),
        ],
    ) -> WorkflowTemplateDetail:
        """
        Retrieve the full architectural blueprint of a workflow template.

        Returns complete element configurations (names, types, approval outcomes/buttons,
        assigned roles, email templates, timeouts) and edge links connecting each step.
        Use this to inspect exact configurations or replicate a template's flow structure.
        """
        return get_template_detail_tool(template_id)
