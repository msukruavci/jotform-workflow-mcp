"""Layer 4: risky write tools."""
from __future__ import annotations

from typing import Annotated

from mcp.server import MCPServer
from pydantic import Field

from mcp_server import graph, revision_log, schema_registry, workflow_inspector
from mcp_server import tree_builder as tb
from mcp_server.jotform_client import JotformAPIError, JotformClient, workflow_revision_id
from mcp_server.models import (
    DeleteStepResult, DeleteWorkflowResult, PublishWorkflowResult,
    RestoreWorkflowRevisionResult,
)


def _workflow_url(workflow_id: str | None) -> str | None:
    return f"https://www.jotform.com/workflow/{workflow_id}/build" if workflow_id else None


INTENT_FIELD = Field(
    description=(
        "Optional short, privacy-conscious summary of the user's intent for "
        "audit/debug logs. Do not copy the full user message; keep one phrase."
    )
)
REASON_FIELD = Field(
    description=(
        "Optional short explanation of why this tool call is the right next "
        "step. Used for audit/debug logs and revision history."
    )
)


def _revision_reason(default: str, intent: str = "", reason: str = "") -> str:
    details = []
    if intent:
        details.append(f"intent={intent}")
    if reason:
        details.append(f"reason={reason}")
    return f"{default} ({'; '.join(details)})" if details else default


def _draft_recipient_placeholders(elements: list[dict]) -> list[str]:
    recipient_keys = {"to", "cc", "bcc", "replyTo", "approver", "assignee", "recipients"}
    found: set[str] = set()

    def visit(value, *, in_recipient: bool = False) -> None:
        if isinstance(value, dict):
            for nested_key, nested in value.items():
                visit(nested, in_recipient=in_recipient or str(nested_key) in recipient_keys)
        elif isinstance(value, list):
            for nested in value:
                visit(nested, in_recipient=in_recipient)
        elif in_recipient and isinstance(value, str):
            lowered = value.lower()
            if "@" in value and (".invalid" in lowered or ".internal" in lowered):
                found.add(value)

    for element in elements:
        visit(element)
    return sorted(found)


def _restore_result_from_revision(
    workflow_id: str,
    revision: dict,
    *,
    needs_confirmation: bool = False,
    restored: bool = False,
    current_backup_revision_id: str | None = None,
    current_revision_id: str | None = None,
    hint: str | None = None,
) -> RestoreWorkflowRevisionResult:
    summary = revision_log.summarize_revision(revision)
    return RestoreWorkflowRevisionResult(
        workflow_id=workflow_id,
        workflow_url=_workflow_url(workflow_id),
        revision_id=summary.get("revision_id"),
        revision_timestamp=summary.get("timestamp"),
        session_id=summary.get("session_id"),
        reason=summary.get("reason"),
        target_title=summary.get("title"),
        target_step_count=summary.get("step_count") or 0,
        target_link_count=summary.get("link_count") or 0,
        current_backup_revision_id=current_backup_revision_id,
        current_revision_id=current_revision_id,
        needs_confirmation=needs_confirmation,
        restored=restored,
        hint=hint,
    )


def _unconnected_branch_outcomes(elements: list[dict]) -> list[str]:
    unconnected: list[str] = []
    for element in elements:
        if element.get("type") not in schema_registry.BRANCHING_TYPES:
            continue
        step_id = element.get("element_id")
        for outcome in element.get("outcomes") or []:
            if not isinstance(outcome, dict):
                continue
            if outcome.get("linkID") in (None, 0, "0", ""):
                label = tb.outcome_label(outcome) or outcome.get("outcomeID")
                unconnected.append(f"step {step_id} {label}")
    return unconnected


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
        intent: Annotated[str, INTENT_FIELD] = "",
        reason: Annotated[str, REASON_FIELD] = "",
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
        source_ids = {
            str(l.get("fromElement")) for l in links
            if l.get("link_id") in incident_link_ids
            and l.get("fromElement") is not None
            and str(l.get("fromElement")) != str(step_id)
        }

        outcome_clears = []
        source_ids_with_cleared_outcomes: set[str] = set()
        for source_id in sorted(source_ids):
            try:
                source = client.get_element(workflow_id, source_id)
            except JotformAPIError as e:
                return DeleteStepResult(
                    step_id=step_id,
                    error=(
                        f"Could not inspect source step {source_id} before "
                        f"deleting; nothing was deleted: {e}"
                    ),
                )
            if source.get("type") not in schema_registry.BRANCHING_TYPES:
                continue
            clear = tb.build_outcome_clears_for_links(source, incident_link_ids)
            if clear is not None:
                outcome_clears.append(clear)
                source_ids_with_cleared_outcomes.add(source_id)

        try:
            revision_log.capture_workflow_revision(
                client,
                workflow_id,
                _revision_reason(f"before delete_step {step_id}", intent, reason),
                tool_name="delete_step",
            )
            client.update_tree(
                workflow_id,
                elements=[
                    *outcome_clears,
                    {"action": "delete", "elementID": step_id,
                     "data": {"element_id": step_id}},
                ],
                links=link_deletes,
            )
        except JotformAPIError as e:
            return DeleteStepResult(step_id=step_id, error=str(e))

        try:
            remaining_elements = client.get_elements(workflow_id)
            remaining_links = client.get_links(workflow_id)
            source_read_backs = {
                source_id: client.get_element(workflow_id, source_id)
                for source_id in source_ids_with_cleared_outcomes
            }
        except JotformAPIError as e:
            return DeleteStepResult(
                step_id=step_id,
                deleted=True,
                error=f"Step deletion completed, but could not verify it: {e}",
            )

        step_removed = not any(
            str(item.get("element_id")) == str(step_id)
            for item in remaining_elements
        )
        links_removed = not any(
            str(item.get("fromElement")) == str(step_id)
            or str(item.get("toElement")) == str(step_id)
            for item in remaining_links
        )
        cleared_link_ids = {str(link_id) for link_id in incident_link_ids}
        outcomes_cleared = all(
            not any(
                str(outcome.get("linkID")) in cleared_link_ids
                for outcome in (source.get("outcomes") or [])
                if isinstance(outcome, dict)
            )
            for source in source_read_backs.values()
        )

        if not step_removed or not links_removed or not outcomes_cleared:
            return DeleteStepResult(
                step_id=step_id,
                deleted=step_removed,
                error=(
                    "Step deletion did not persist completely; inspect the "
                    "workflow before retrying."
                ),
                hint=(
                    f"step_removed={step_removed}, links_removed={links_removed}, "
                    f"outcomes_cleared={outcomes_cleared}"
                ),
            )

        return DeleteStepResult(step_id=step_id, deleted=True, verified=True)

    @mcp.tool()
    def publish_workflow(
        workflow_id: Annotated[str, Field(
            description=(
                "Workflow ID to enable. Do not call this immediately after "
                "build_workflow_bulk. First call show_workflow and answer with "
                "the workflow/form links; call publish_workflow only if the user "
                "explicitly asks to enable or publish."
            )
        )],
        confirm: Annotated[bool, Field(
            description=(
                "Leave false for the enable preview, but only call even the preview "
                "after the user explicitly asks to enable/publish. Set true only "
                "after the user explicitly confirms that this disabled workflow "
                "should start running."
            )
        )] = False,
        expected_revision_id: Annotated[str, Field(
            description=(
                "Required with confirm=true. Echo revision_id from the immediately preceding "
                "confirm=false preview so a changed workflow cannot be enabled accidentally."
            )
        )] = "",
        allow_draft_recipients: Annotated[bool, Field(
            description=(
                "Default false. Set true only if the user explicitly accepts enabling "
                "with .invalid/.internal draft recipient placeholders after seeing the "
                "warnings. This can route workflow notifications to placeholder addresses."
            )
        )] = False,
        intent: Annotated[str, INTENT_FIELD] = "",
        reason: Annotated[str, REASON_FIELD] = "",
    ) -> PublishWorkflowResult:
        """
        Enable a disabled workflow, making it live — from this point on,
        matching form submissions can run it.

        Do not use this as the normal final step after creating or editing a
        workflow. First call show_workflow and answer with the workflow/form
        links. Use publish_workflow only when the user explicitly asks to
        enable or publish. The first call with confirm=false only previews the
        status change and any advisory health warnings. Call again with
        confirm=true only after the user explicitly agrees. Draft recipient
        placeholders are warned; enabling with them requires the user to accept
        the warning and allow_draft_recipients=true. Workflows are intentionally
        left DISABLED after every build_workflow_bulk write.
        """
        try:
            combined = client.get_workflow_combined(workflow_id)
        except JotformAPIError as e:
            return PublishWorkflowResult(workflow_id=workflow_id, error=str(e))

        elements = [e for e in (combined.get("elements") or []) if isinstance(e, dict)]
        links = [l for l in (combined.get("links") or []) if isinstance(l, dict)]
        steps = [{"step_id": e.get("element_id"), "type": e.get("type")} for e in elements]
        conns = [{"link_id": l.get("link_id"), "from_step": l.get("fromElement"),
                 "to_step": l.get("toElement")} for l in links]
        health = graph.analyse(steps, conns)
        branch_health = workflow_inspector.branch_diagnostics(elements, links)
        workflow_meta = combined.get("workflow") if isinstance(combined.get("workflow"), dict) else {}
        current_status = str(workflow_meta.get("status") or "").upper() or None
        current_revision_id = workflow_revision_id(combined)

        warnings = []
        if health["unreachable_steps"]:
            warnings.append(f"{len(health['unreachable_steps'])} step(s) can never run: "
                           f"{health['unreachable_steps']}")
        if health["dead_end_steps"]:
            warnings.append(f"{len(health['dead_end_steps'])} step(s) lead nowhere: "
                           f"{health['dead_end_steps']}")
        if health["dangling_links"]:
            warnings.append(f"broken link(s): {health['dangling_links']}")
        unconnected_branches = _unconnected_branch_outcomes(elements)
        if unconnected_branches:
            warnings.append(f"unconnected branch outcome(s): {unconnected_branches}")
        if branch_health["unlabelled_branching_steps"]:
            warnings.append(
                "unlabelled branching link(s): "
                f"{branch_health['unlabelled_branching_steps']}"
            )
        if branch_health["invalid_branch_links"]:
            warnings.append(
                f"invalid branch mapping(s): {branch_health['invalid_branch_links']}"
            )
        placeholders = _draft_recipient_placeholders(elements)
        if placeholders:
            warnings.append(
                "draft recipient placeholder(s) present before enabling: "
                + ", ".join(placeholders)
            )

        if current_status == "ENABLED":
            return PublishWorkflowResult(
                workflow_id=workflow_id,
                workflow_url=_workflow_url(workflow_id),
                current_status=current_status,
                revision_id=current_revision_id,
                health_warnings=warnings,
                published=True,
                hint="Workflow is already ENABLED; no status change was made.",
            )

        if not confirm:
            return PublishWorkflowResult(
                workflow_id=workflow_id,
                workflow_url=_workflow_url(workflow_id),
                current_status=current_status,
                revision_id=current_revision_id,
                needs_confirmation=True,
                health_warnings=warnings,
                hint=(
                    "Show the current DISABLED status and advisory warnings to the user. "
                    "Call publish_workflow again with confirm=true and expected_revision_id set "
                    "to this revision_id only if they explicitly want it to start running."
                ),
            )

        if not expected_revision_id:
            return PublishWorkflowResult(
                workflow_id=workflow_id,
                workflow_url=_workflow_url(workflow_id),
                current_status=current_status,
                revision_id=current_revision_id,
                health_warnings=warnings,
                error="expected_revision_id is required when confirm=true.",
                hint="Preview again with confirm=false and echo its revision_id exactly.",
            )
        if expected_revision_id != current_revision_id:
            return PublishWorkflowResult(
                workflow_id=workflow_id,
                workflow_url=_workflow_url(workflow_id),
                current_status=current_status,
                revision_id=current_revision_id,
                health_warnings=warnings,
                error="Workflow changed after the publish preview; it was not enabled.",
                hint="Preview again and ask for confirmation against the new revision_id.",
            )
        if placeholders and not allow_draft_recipients:
            return PublishWorkflowResult(
                workflow_id=workflow_id,
                workflow_url=_workflow_url(workflow_id),
                current_status=current_status,
                revision_id=current_revision_id,
                health_warnings=warnings,
                needs_confirmation=True,
                error="Draft recipient placeholders need explicit override before publishing.",
                hint=(
                    "Recommended: replace every .invalid/.internal recipient, then preview publishing again. "
                    "If the user explicitly accepts the risk, call publish_workflow with confirm=true, "
                    "the same expected_revision_id, and allow_draft_recipients=true."
                ),
            )

        try:
            revision_log.capture_workflow_revision(
                client,
                workflow_id,
                _revision_reason("before publish_workflow", intent, reason),
                tool_name="publish_workflow",
            )
            publish_result = client.publish_workflow(workflow_id)
        except JotformAPIError as e:
            return PublishWorkflowResult(
                workflow_id=workflow_id,
                workflow_url=_workflow_url(workflow_id),
                current_status=current_status,
                health_warnings=warnings,
                error=str(e),
            )

        enabled_status = (
            str(publish_result.get("status") or "").upper()
            if isinstance(publish_result, dict)
            else ""
        )
        if enabled_status != "ENABLED":
            try:
                live = client.get_workflow(workflow_id)
                enabled_status = str(live.get("status") or "").upper()
            except JotformAPIError as e:
                return PublishWorkflowResult(
                    workflow_id=workflow_id,
                    workflow_url=_workflow_url(workflow_id),
                    current_status=current_status,
                    health_warnings=warnings,
                    error=f"Enable request completed but status verification failed: {e}",
                )
        if enabled_status != "ENABLED":
            return PublishWorkflowResult(
                workflow_id=workflow_id,
                workflow_url=_workflow_url(workflow_id),
                current_status=enabled_status or current_status,
                health_warnings=warnings,
                error="Workflow enable request did not persist as ENABLED.",
            )

        return PublishWorkflowResult(
            workflow_id=workflow_id,
            workflow_url=_workflow_url(workflow_id),
            current_status="ENABLED",
            revision_id=current_revision_id,
            health_warnings=warnings,
            published=True,
        )


    @mcp.tool()
    def restore_workflow_revision(
        workflow_id: Annotated[str, Field(description="From list_workflows.")],
        revision_id: Annotated[str, Field(
            description=(
                "For preview, may be empty to select the newest saved revision. "
                "With confirm=true it is required and must exactly match the "
                "revision_id returned by that preview."
            )
        )] = "",
        confirm: Annotated[bool, Field(
            description=(
                "Leave false to preview the restore target. Only pass true "
                "after showing that preview to the user and getting explicit "
                "approval. The current workflow is backed up as a new revision "
                "before restore."
            )
        )] = False,
        expected_current_revision_id: Annotated[str, Field(
            description=(
                "Required with confirm=true. Echo current_revision_id from the restore preview "
                "so concurrent workflow edits cannot be overwritten."
            )
        )] = "",
        intent: Annotated[str, INTENT_FIELD] = "",
        reason: Annotated[str, REASON_FIELD] = "",
    ) -> RestoreWorkflowRevisionResult:
        """
        Restore a workflow to a saved revision.

        Revisions are captured automatically before add/update/connect/
        disconnect/delete/publish operations. Without revision_id, this uses
        the newest saved revision, so it is the "go back one change" tool.
        """
        if confirm and not revision_id:
            return RestoreWorkflowRevisionResult(
                workflow_id=workflow_id,
                workflow_url=_workflow_url(workflow_id),
                error="revision_id is required when confirm=true.",
                hint="Preview again with confirm=false and echo its revision_id exactly.",
            )

        revision = revision_log.load_workflow_revision(workflow_id, revision_id or None)
        if revision is None:
            return RestoreWorkflowRevisionResult(
                workflow_id=workflow_id,
                workflow_url=_workflow_url(workflow_id),
                error=(
                    f"No revision found for workflow {workflow_id}"
                    + (f" with revision_id {revision_id!r}." if revision_id else ".")
                ),
                hint=(
                    "Revisions are created automatically before mutating tool "
                    "calls. Call list_workflow_revisions to see what exists."
                ),
            )

        try:
            current_snapshot = client.get_workflow_combined(workflow_id)
            current_revision_id = workflow_revision_id(current_snapshot)
        except JotformAPIError as error:
            return RestoreWorkflowRevisionResult(
                workflow_id=workflow_id,
                workflow_url=_workflow_url(workflow_id),
                revision_id=revision.get("revision_id"),
                error=f"Could not bind the restore to the current live workflow: {error}",
            )

        if not confirm:
            return _restore_result_from_revision(
                workflow_id,
                revision,
                needs_confirmation=True,
                current_revision_id=current_revision_id,
                hint=(
                    "Show this target revision to the user. Call again with confirm=true "
                    "with this exact revision_id and current_revision_id only if they explicitly say to restore it."
                ),
            )

        if not expected_current_revision_id:
            return RestoreWorkflowRevisionResult(
                workflow_id=workflow_id,
                workflow_url=_workflow_url(workflow_id),
                revision_id=revision.get("revision_id"),
                current_revision_id=current_revision_id,
                error="expected_current_revision_id is required when confirm=true.",
                hint="Preview again and echo both revision identifiers exactly.",
            )
        if expected_current_revision_id != current_revision_id:
            return RestoreWorkflowRevisionResult(
                workflow_id=workflow_id,
                workflow_url=_workflow_url(workflow_id),
                revision_id=revision.get("revision_id"),
                current_revision_id=current_revision_id,
                error="Workflow changed after the restore preview; no restore was attempted.",
                hint="Preview the restore again against the latest workflow revision.",
            )

        backup = None
        try:
            backup = revision_log.capture_workflow_revision(
                client,
                workflow_id,
                _revision_reason(
                    f"before restore_workflow_revision {revision.get('revision_id')}",
                    intent,
                    reason,
                ),
                tool_name="restore_workflow_revision",
            )
            revision_log.restore_workflow_revision(client, workflow_id, revision)
        except (JotformAPIError, RuntimeError, ValueError) as e:
            return RestoreWorkflowRevisionResult(
                workflow_id=workflow_id,
                workflow_url=_workflow_url(workflow_id),
                revision_id=revision.get("revision_id"),
                current_backup_revision_id=(backup or {}).get("revision_id"),
                error=str(e),
            )

        return _restore_result_from_revision(
            workflow_id,
            revision,
            restored=True,
            current_backup_revision_id=backup.get("revision_id") if backup else None,
            current_revision_id=current_revision_id,
        )

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
        intent: Annotated[str, INTENT_FIELD] = "",
        reason: Annotated[str, REASON_FIELD] = "",
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
            revision_log.capture_workflow_revision(
                client,
                workflow_id,
                _revision_reason("before delete_workflow", intent, reason),
                tool_name="delete_workflow",
            )
            client.delete_workflow(workflow_id)
        except JotformAPIError as e:
            return DeleteWorkflowResult(workflow_id=workflow_id, title=title, error=str(e))

        return DeleteWorkflowResult(workflow_id=workflow_id, title=title, deleted=True)
