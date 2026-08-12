"""
Layer 3: building.

Every tool here follows the same shape: fetch current state (never trust a
cached id or position from earlier in the conversation), ask tree_builder
what to send, write it, report what happened in terms the model can act on.

None of these tools accept x/y, port names, or link `type` from the model —
those are either server-computed (ports), constant (link type — see
tree_builder for why a typo there is dangerous), or not yet solved
(layout; see docs/gap-report.md item 5). What a step *is* is the model's
job; where it sits on the canvas is ours.
"""
from __future__ import annotations

from typing import Annotated

from mcp.server import MCPServer
from pydantic import Field

from mcp_server import schema_registry, tree_builder as tb
from mcp_server.jotform_client import JotformAPIError, JotformClient
from mcp_server.models import (
    AddStepResult, ConnectStepsResult, CreateWorkflowResult,
    DisconnectStepsResult, UpdateStepResult,
)


def register(mcp: MCPServer, client: JotformClient) -> None:
    @mcp.tool()
    def create_workflow(
        title: Annotated[str, Field(description="Workflow name.")],
        trigger_form_id: Annotated[str, Field(
            description=(
                "Optional — the form (from list_forms) whose submissions "
                "should trigger this workflow. Binding takes two API calls "
                "under the hood (see JotformClient.set_trigger_form) and "
                "the result is verified by reading the start point back — "
                "never trusted from the write response alone. If "
                "verification fails, the workflow is still created and the "
                "result explains the user needs to set the trigger form "
                "manually in the Jotform builder (Settings -> trigger "
                "form, or drag a form onto the start point)."
            )
        )] = "",
    ) -> CreateWorkflowResult:
        """
        Create a new, empty workflow with a start point.

        Returns the new workflow_id. Use add_step to start adding steps.
        """
        try:
            created = client.create_workflow(title)
        except JotformAPIError as e:
            return CreateWorkflowResult(error=str(e))

        workflow_id = created.get("id") or created.get("workflowID")
        if not workflow_id:
            return CreateWorkflowResult(error=f"No workflow id in response: {created!r}")

        if trigger_form_id:
            try:
                client.set_trigger_form(workflow_id, trigger_form_id)
            except JotformAPIError as e:
                return CreateWorkflowResult(
                    workflow_id=str(workflow_id), title=title,
                    error=f"Workflow created, but setting trigger form failed: {e}",
                )

            # Never trust a write response alone (decision-log rule: no
            # boolean flag or 200 counts as proof of an effect) — read the
            # start point back and confirm the form id actually landed.
            # Element 1 is always the start point (jotform_client.create_workflow
            # creates it that way, and set_trigger_form always targets
            # elementID 1) — read it with get_element, not get_elements:
            # per jotform_client's own docstring, the plural/list endpoint
            # only summarizes, the singular endpoint returns the full
            # config. Verifying against the summary risks a false "could
            # not be verified" if resourceID happens to be one of the
            # fields the summary omits.
            try:
                start = client.get_element(workflow_id, 1)
            except JotformAPIError as e:
                return CreateWorkflowResult(
                    workflow_id=str(workflow_id), title=title,
                    error=f"Workflow created, trigger form set, but could not verify: {e}",
                )

            if str(start.get("resourceID")) != str(trigger_form_id):
                return CreateWorkflowResult(
                    workflow_id=str(workflow_id), title=title,
                    error=(
                        "Workflow created, but the trigger form binding could "
                        "not be verified — the start point doesn't show this "
                        "form id after the write. Tell the user to check the "
                        "trigger form in the Jotform builder (Settings -> "
                        "trigger form) and set it manually if it's missing."
                    ),
                )

        return CreateWorkflowResult(
            workflow_id=str(workflow_id), title=title,
            trigger_form_id=trigger_form_id or None,
        )

    @mcp.tool()
    def add_step(
        workflow_id: Annotated[str, Field(description="From list_workflows.")],
        step_type: Annotated[str, Field(
            description='From list_step_types, e.g. "workflow_send_email".'
        )],
        config: Annotated[dict, Field(
            description=(
                "Fields for this step type — call get_step_schema first to "
                "see what it accepts. Unknown fields are dropped, not "
                "rejected; check `warnings` in the result."
            )
        )],
        after_step_id: Annotated[str, Field(
            description=(
                "Optional — if given, connects this step directly after "
                "that one. Only works when after_step_id doesn't already "
                "have an outgoing connection (a step with more than one "
                "exit needs deliberate wiring — use connect_steps for "
                "that, with an outcome if the source branches)."
            )
        )] = "",
    ) -> AddStepResult:
        """
        Add a step to a workflow.

        Returns the new step_id. Position on the canvas is chosen
        automatically.
        """
        try:
            clean_config, warnings = tb.validate_config(step_type, config)
        except tb.ValidationError as e:
            return AddStepResult(
                error=str(e), hint="Call list_step_types to see valid values.",
            )

        try:
            elements = client.get_elements(workflow_id)
        except JotformAPIError as e:
            return AddStepResult(error=str(e))

        after_id = after_step_id or None
        if after_id is not None:
            try:
                links = client.get_links(workflow_id)
            except JotformAPIError as e:
                return AddStepResult(error=str(e))
            existing_exit = next(
                (l for l in links if str(l.get("fromElement")) == str(after_id)), None
            )
            if existing_exit is not None:
                return AddStepResult(
                    error=(
                        f"Step {after_id} already has an outgoing connection "
                        f"(to step {existing_exit.get('toElement')})."
                    ),
                    hint=(
                        "Add this step without after_step_id, then use "
                        "connect_steps to wire it in explicitly — pass an "
                        "outcome if step {after_id} is an if/else or "
                        "conditional branch."
                    ).format(after_id=after_id),
                )

        element_id = tb.next_id([e.get("element_id") for e in elements])
        position = tb.compute_position(elements, after_id)
        create_entry = tb.build_element_create(step_type, element_id, clean_config, position)

        try:
            client.update_tree(workflow_id, elements=[create_entry])
        except JotformAPIError as e:
            return AddStepResult(error=str(e))

        linked_from = None
        if after_id is not None:
            try:
                links = client.get_links(workflow_id)
                link_id = tb.next_id([l.get("link_id") for l in links])
                client.update_tree(
                    workflow_id,
                    links=[tb.build_link_create(link_id, after_id, element_id)],
                )
                linked_from = str(after_id)
            except JotformAPIError as e:
                return AddStepResult(
                    step_id=str(element_id), type=step_type, warnings=warnings,
                    error=f"Step created, but linking from {after_id} failed: {e}",
                )

        return AddStepResult(
            step_id=str(element_id), type=step_type,
            linked_from=linked_from, warnings=warnings,
        )

    @mcp.tool()
    def connect_steps(
        workflow_id: Annotated[str, Field(description="From list_workflows.")],
        from_step_id: Annotated[str, Field(description="From get_workflow's steps list.")],
        to_step_id: Annotated[str, Field(description="From get_workflow's steps list.")],
        outcome: Annotated[str, Field(
            description=(
                'Required if from_step_id is an if/else or conditional '
                'branch step — e.g. "TRUE", "FALSE", or a custom branch '
                "name. Check get_workflow's connections or "
                "get_step_details on from_step_id to see what outcomes "
                "exist and which are already used. Leave empty for "
                "non-branching steps."
            )
        )] = "",
    ) -> ConnectStepsResult:
        """
        Connect two existing steps.

        Fails without changing anything if the outcome doesn't exist, is
        already connected elsewhere, or is missing when required.
        """
        try:
            source = client.get_element(workflow_id, from_step_id)
        except JotformAPIError as e:
            return ConnectStepsResult(error=str(e))

        source_type = source.get("type")
        is_branching = source_type in schema_registry.BRANCHING_TYPES

        if is_branching and not outcome:
            # tb.outcome_label, not raw conditionValue — a conditional
            # branch's named outcomes all share conditionValue "CUSTOM";
            # the real per-branch name lives in branchName, and this hint
            # is the model's only way to discover it without a separate
            # get_step_details call.
            available = [tb.outcome_label(o) for o in (source.get("outcomes") or [])]
            return ConnectStepsResult(
                error=f"{from_step_id} is a {source_type} and requires an outcome.",
                hint=f"Available outcomes: {available}",
            )
        if not is_branching and outcome:
            return ConnectStepsResult(
                error=f"{from_step_id} ({source_type}) does not branch — it takes no outcome.",
            )

        matched_outcome = None
        if is_branching:
            try:
                matched_outcome = tb.resolve_outcome(source, outcome)
            except tb.ValidationError as e:
                return ConnectStepsResult(error=str(e))

        try:
            links = client.get_links(workflow_id)
        except JotformAPIError as e:
            return ConnectStepsResult(error=str(e))

        link_id = tb.next_id([l.get("link_id") for l in links])
        try:
            client.update_tree(
                workflow_id,
                links=[tb.build_link_create(link_id, from_step_id, to_step_id)],
            )
        except JotformAPIError as e:
            return ConnectStepsResult(error=str(e))

        if is_branching:
            try:
                client.update_tree(
                    workflow_id,
                    elements=[tb.build_outcome_update(
                        source, matched_outcome["outcomeID"], link_id
                    )],
                )
            except JotformAPIError as e:
                return ConnectStepsResult(
                    link_id=str(link_id), from_step=from_step_id, to_step=to_step_id,
                    error=(
                        f"Link created, but labelling the outcome failed: {e}. "
                        f"The steps are connected but the branch is unlabelled."
                    ),
                )

        return ConnectStepsResult(
            link_id=str(link_id), from_step=from_step_id, to_step=to_step_id,
            outcome=outcome or None,
        )

    @mcp.tool()
    def disconnect_steps(
        workflow_id: Annotated[str, Field(description="From list_workflows.")],
        link_id: Annotated[str, Field(
            description="From get_workflow's connections list."
        )],
    ) -> DisconnectStepsResult:
        """
        Remove a single connection between two steps, without deleting
        either step.

        If the connection leaves a branching step (if/else, conditional
        branch, approval), that outcome's link is cleared first — so it
        shows up as unconnected again and can be wired to something else
        with connect_steps. Without this, the outcome would still point at
        a link_id that no longer exists: resolve_outcome would wrongly
        report it as already connected, and get_workflow's health check
        would separately start flagging it as a dangling link.

        Use this instead of add_step + connect_steps when the goal is
        rewiring an existing structure — for example, replacing
        On Submission -> Review -> Approval with a direct
        On Submission -> Approval by removing the old link first.
        """
        try:
            links = client.get_links(workflow_id)
        except JotformAPIError as e:
            return DisconnectStepsResult(error=str(e))

        link = next((l for l in links if str(l.get("link_id")) == str(link_id)), None)
        if link is None:
            return DisconnectStepsResult(
                error=f"No link {link_id} in this workflow.",
                hint="Call get_workflow and check the connections list for valid link ids.",
            )

        from_step_id = link.get("fromElement")

        try:
            source = client.get_element(workflow_id, from_step_id)
        except JotformAPIError as e:
            return DisconnectStepsResult(error=str(e))

        outcome_cleared = None
        if source.get("type") in schema_registry.BRANCHING_TYPES:
            outcome = tb.find_outcome_by_link(source, link_id)
            if outcome is not None:
                try:
                    client.update_tree(
                        workflow_id,
                        elements=[tb.build_outcome_update(
                            source, outcome["outcomeID"], None
                        )],
                    )
                except JotformAPIError as e:
                    return DisconnectStepsResult(
                        from_step=str(from_step_id),
                        error=f"Could not clear the outcome before disconnecting: {e}",
                    )
                outcome_cleared = tb.outcome_label(outcome)

        try:
            client.update_tree(workflow_id, links=[tb.build_link_delete(link_id)])
        except JotformAPIError as e:
            return DisconnectStepsResult(
                from_step=str(from_step_id), outcome_cleared=outcome_cleared,
                error=(
                    f"Outcome cleared but link deletion failed: {e}. "
                    f"The branch is now unwired but the old link may still exist "
                    f"— check get_workflow before retrying."
                ),
            )

        return DisconnectStepsResult(
            link_id=str(link_id), from_step=str(from_step_id),
            outcome_cleared=outcome_cleared, disconnected=True,
        )

    @mcp.tool()
    def update_step(
        workflow_id: Annotated[str, Field(description="From list_workflows.")],
        step_id: Annotated[str, Field(description="From get_workflow's steps list.")],
        config: Annotated[dict, Field(
            description=(
                "Only the fields to change — call get_step_details first "
                "to see current values, get_step_schema for valid fields."
            )
        )],
    ) -> UpdateStepResult:
        """
        Change an existing step's configuration.

        Does not move the step or change its connections — use connect_steps
        for wiring.
        """
        try:
            current = client.get_element(workflow_id, step_id)
        except JotformAPIError as e:
            return UpdateStepResult(step_id=step_id, error=str(e))

        step_type = current.get("type")
        if not step_type:
            return UpdateStepResult(step_id=step_id, error="Could not determine this step's type.")

        try:
            clean_config, warnings = tb.validate_config(step_type, config)
        except tb.ValidationError as e:
            return UpdateStepResult(step_id=step_id, error=str(e))

        if not clean_config:
            return UpdateStepResult(
                step_id=step_id, warnings=warnings,
                error="Nothing to update — no valid fields in config.",
            )

        try:
            client.update_tree(
                workflow_id, elements=[tb.build_element_update(step_id, clean_config)]
            )
        except JotformAPIError as e:
            return UpdateStepResult(step_id=step_id, warnings=warnings, error=str(e))

        return UpdateStepResult(step_id=step_id, warnings=warnings)