"""
Layer 4: risky.

Every tool here follows a two-call pattern: call once and nothing happens —
you get back what *would* happen. Call again with confirm=True and it does.
This is not a suggestion to the model, it's the only way these tools work:
there is no single call that both previews and acts.

Why this shape and not a yes/no prompt inside the tool: MCP tools are
synchronous request/response, there's no channel to pause mid-call and wait
for a person to answer. Forcing two calls means the model must have shown
the preview and gotten an explicit go-ahead in the conversation before
confirm=True can mean anything — a model that fabricates confirmation is
lying to the person, not working around a technical limitation.
"""
from __future__ import annotations

from typing import Annotated

from mcp.server import MCPServer
from pydantic import Field

from mcp_server import graph, schema_registry
from mcp_server.jotform_client import JotformAPIError, JotformClient
from mcp_server.models import DeleteStepResult, DeleteWorkflowResult, PublishWorkflowResult


def register(mcp: MCPServer, client: JotformClient) -> None:
    @mcp.tool()
    def delete_step(
        workflow_id: Annotated[str, Field(description="From list_workflows.")],
        step_id: Annotated[str, Field(description="From get_workflow's steps list.")],
        confirm: Annotated[bool, Field(
            description=(
                "Leave false to preview what deleting this step would "
                "affect. Only pass true after showing that preview to the "
                "user and getting their explicit go-ahead — never set this "
                "based on your own judgement that it's probably fine."
            )
        )] = False,
    ) -> DeleteStepResult:
        """
        Delete a step from a workflow. Irreversible.

        First call (confirm=false) changes nothing and returns the step's
        type, label, and every connection that would break. Second call
        (confirm=true) actually deletes it.

        Confirmed (probes/test_delete_impact.py, 2026-08-10): Jotform does
        NOT remove a step's connections when the step is deleted — this
        tool deletes them explicitly, in the same request, so no broken
        links are left behind.
        """
        try:
            element = client.get_element(workflow_id, step_id)
        except JotformAPIError as e:
            return DeleteStepResult(step_id=step_id, error=str(e))

        if not confirm:
            try:
                links = client.get_links(workflow_id)
            except JotformAPIError as e:
                return DeleteStepResult(step_id=step_id, error=str(e))

            affected = []
            for link in links:
                if str(link.get("fromElement")) == str(step_id):
                    affected.append(f"this step -> step {link.get('toElement')} will be broken")
                elif str(link.get("toElement")) == str(step_id):
                    affected.append(f"step {link.get('fromElement')} -> this step will be broken")

            return DeleteStepResult(
                step_id=step_id,
                type=element.get("type"),
                label=element.get("name") or schema_registry.default_label(element.get("type")),
                needs_confirmation=True,
                affected_connections=affected,
                hint=(
                    "Show this to the user. Call again with confirm=true "
                    "only if they explicitly say to proceed."
                ),
            )

        try:
            links = client.get_links(workflow_id)
        except JotformAPIError as e:
            return DeleteStepResult(step_id=step_id, error=str(e))

        # Confirmed: links are not cascade-deleted. Remove every link
        # touching this step in the same request as the element, so the
        # delete can't complete "successfully" and still leave dangling
        # links behind.
        incident_link_ids = [
            l.get("link_id") for l in links
            if str(l.get("fromElement")) == str(step_id)
            or str(l.get("toElement")) == str(step_id)
        ]
        link_deletes = [
            {"action": "delete", "linkID": lid, "data": {"link_id": lid}}
            for lid in incident_link_ids
        ]

        try:
            client.update_tree(
                workflow_id,
                elements=[{"action": "delete", "elementID": step_id,
                          "data": {"element_id": step_id}}],
                links=link_deletes,
            )
        except JotformAPIError as e:
            return DeleteStepResult(step_id=step_id, error=str(e))

        return DeleteStepResult(step_id=step_id, deleted=True)

    @mcp.tool()
    def publish_workflow(
        workflow_id: Annotated[str, Field(description="From list_workflows.")],
        confirm: Annotated[bool, Field(
            description=(
                "Leave false to check the workflow's structure and see "
                "what publishing would do. Only pass true after showing "
                "that to the user and getting their explicit go-ahead."
            )
        )] = False,
    ) -> PublishWorkflowResult:
        """
        Publish a workflow, making it live — from this point on, matching
        form submissions actually run it.

        First call (confirm=false) changes nothing. It runs the same health
        check as get_workflow — unreachable steps, dead ends, unlabelled
        branches — and returns them as warnings. A workflow with warnings
        can still be published; the warnings exist so the user finds out
        about a broken branch from you, before it goes live, rather than
        from a submission that silently went nowhere.
        """
        try:
            combined = client.get_workflow_combined(workflow_id)
        except JotformAPIError as e:
            return PublishWorkflowResult(workflow_id=workflow_id, error=str(e))

        if not confirm:
            elements = [e for e in (combined.get("elements") or []) if isinstance(e, dict)]
            links = [l for l in (combined.get("links") or []) if isinstance(l, dict)]
            steps = [{"step_id": e.get("element_id"), "type": e.get("type")} for e in elements]
            conns = [{"link_id": l.get("link_id"), "from_step": l.get("fromElement"),
                     "to_step": l.get("toElement")} for l in links]
            health = graph.analyse(steps, conns)

            warnings = []
            if health["unreachable_steps"]:
                warnings.append(f"{len(health['unreachable_steps'])} step(s) can never run: "
                               f"{health['unreachable_steps']}")
            if health["dead_end_steps"]:
                warnings.append(f"{len(health['dead_end_steps'])} step(s) lead nowhere: "
                               f"{health['dead_end_steps']}")
            if health["dangling_links"]:
                warnings.append(f"broken link(s): {health['dangling_links']}")

            return PublishWorkflowResult(
                workflow_id=workflow_id, needs_confirmation=True,
                health_warnings=warnings,
                hint=(
                    "Show this to the user, warnings included even if empty. "
                    "Call again with confirm=true only if they say to proceed."
                ),
            )

        try:
            client.publish_workflow(workflow_id)
        except JotformAPIError as e:
            return PublishWorkflowResult(workflow_id=workflow_id, error=str(e))

        return PublishWorkflowResult(workflow_id=workflow_id, published=True)


    @mcp.tool()
    def delete_workflow(
        workflow_id: Annotated[str, Field(description="From list_workflows.")],
        confirm: Annotated[bool, Field(
            description="Leave false to preview. Only true after the user "
                        "explicitly says to proceed."
        )] = False,
        confirm_title: Annotated[str, Field(
            description=(
                "Required alongside confirm=true — must exactly match the "
                "workflow's current title (returned in the preview). This "
                "exists because workflow titles are often similar or "
                "duplicated; matching by id alone made it easy to delete "
                "the wrong one by picking the wrong list index. Read the "
                "title back to the user and use what they confirm, don't "
                "retype it from memory."
            )
        )] = "",
    ) -> DeleteWorkflowResult:
        """
        Delete an entire workflow. Irreversible, and bigger than delete_step
        — this removes every step in it.

        First call (confirm=false) changes nothing and returns the title
        to show the user. Second call needs both confirm=true and
        confirm_title matching exactly.
        """
        try:
            meta = client.get_workflow(workflow_id)
        except JotformAPIError as e:
            return DeleteWorkflowResult(workflow_id=workflow_id, error=str(e))

        title = meta.get("title")

        if not confirm:
            return DeleteWorkflowResult(
                workflow_id=workflow_id, title=title, needs_confirmation=True,
                hint=(
                    f"Show the title '{title}' to the user. Call again with "
                    f"confirm=true and confirm_title='{title}' only if they "
                    f"explicitly confirm THIS workflow, by name."
                ),
            )

        if confirm_title != title:
            return DeleteWorkflowResult(
                workflow_id=workflow_id, title=title,
                error=(
                    f"confirm_title ({confirm_title!r}) does not match this "
                    f"workflow's actual title ({title!r}). Not deleted."
                ),
            )

        try:
            client.delete_workflow(workflow_id)
        except JotformAPIError as e:
            return DeleteWorkflowResult(workflow_id=workflow_id, title=title, error=str(e))

        return DeleteWorkflowResult(workflow_id=workflow_id, title=title, deleted=True)