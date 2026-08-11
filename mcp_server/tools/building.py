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
    AddStepResult, ConnectStepsResult, CreateWorkflowResult, UpdateStepResult,
)


def register(mcp: MCPServer, client: JotformClient) -> None:
    @mcp.tool()
    def create_workflow(
        title: Annotated[str, Field(description="Workflow name.")],
        trigger_form_id: Annotated[str, Field(
            description=(
                "Optional — the form (from list_forms) whose submissions "
                "should trigger this workflow. Confirmed 2026-08-10: binding "
                "a trigger form is NOT possible through the public API — "
                "the call reports success but changes nothing. If you pass "
                "this and it can't be bound, the workflow is still created "
                "and the result explains the user must set it manually in "
                "the Jotform builder (Settings -> trigger form, or drag a "
                "form onto the start point)."
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
            # Known no-op on the public API (see docstring). Still call it —
            # in case Jotform ever fixes this — but never trust the response
            # alone. Read the start point back and check whether the form id
            # actually landed before claiming success.
            try:
                client.set_trigger_form(workflow_id, trigger_form_id)
                elements = client.get_elements(workflow_id)
                start = next(
                    (e for e in elements if e.get("type") == "workflow_start_point"), {}
                )
                if str(start.get("resourceID")) != str(trigger_form_id):
                    return CreateWorkflowResult(
                        workflow_id=str(workflow_id), title=title,
                        error=(
                            "Workflow created, but the trigger form could not be "
                            "bound — this is a known limitation of the public API, "
                            "not a failure you can retry. Tell the user the "
                            "workflow was created and they need to set the "
                            "trigger form themselves in the Jotform builder."
                        ),
                    )
            except JotformAPIError as e:
                return CreateWorkflowResult(
                    workflow_id=str(workflow_id), title=title,
                    error=f"Workflow created, but setting trigger form failed: {e}",
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
            available = [o.get("conditionValue") for o in (source.get("outcomes") or [])]
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