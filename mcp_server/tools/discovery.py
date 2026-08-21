"""
Layer 1: discovery.

These exist so the other 30+ step types don't have to be crammed into every
tool description. The model asks what's available, then pulls the one schema
it actually needs. Keeps context small and keeps the tool surface stable as
Jotform adds step types.
"""
from __future__ import annotations

from typing import Annotated

from mcp.server import MCPServer
from pydantic import Field

from mcp_server import schema_registry
from mcp_server.models import SchemaField, StepSchema, StepTypeList, StepTypeSummary


def _schema_for_step_type(step_type: str) -> StepSchema:
    step_type = str(step_type or "").strip()
    result = schema_registry.get_simplified_schema(step_type)
    if result is None:
        available = [t["step_type"] for t in schema_registry.list_types()]
        known = step_type in available
        # A type can be real and still have no schema here. Saying
        # "unknown" in that case would be a lie the model then repeats to
        # the user, so the two cases get different messages.
        if known:
            return StepSchema(
                step_type=step_type,
                ui_name=schema_registry.get_ui_name(step_type),
                error=f"No field schema on record for {step_type}.",
                hint=(
                    "This is a real step type and may appear in existing "
                    "workflows, but this server cannot describe or configure "
                    "its fields. Tell the user it must be edited in Jotform."
                ),
            )
        return StepSchema(
            step_type=step_type or None,
            error=f"Unknown step type: {step_type}",
            hint="Call list_step_types to see valid values.",
            available_types=available,
        )
    return StepSchema(
        step_type=result["step_type"],
        canonical_type=result.get("canonical_type"),
        subtype=result.get("subtype"),
        description=result["description"],
        ui_name=result["ui_name"],
        fields=[SchemaField(**f) for f in result["fields"]],
    )


def register(mcp: MCPServer) -> None:
    @mcp.tool()
    def list_step_types(
        category: Annotated[str, Field(
            description='Optional filter — "basic" (email, task, approval), '
                        '"logic" (conditions, branching, loops), "ai" '
                        '(AI-powered steps), or "integration" (webhooks, '
                        "payments, signing). Leave empty to see all."
        )] = "",
    ) -> StepTypeList:
        """
        List the workflow step types you can add to a workflow.

        Each entry gives an add_step step_type, the name the Jotform builder
        shows for it (ui_name), and a one-line description. Some UI entries
        are variants with a canonical_type and subtype; pass the listed
        step_type directly to add_step.

        schema_available=false means this server has no field schema for that
        type: it can appear in an existing workflow, but get_step_schema will
        not describe it. Call get_step_schema on a type before configuring it.
        """
        return StepTypeList(
            step_types=[StepTypeSummary(**t) for t in schema_registry.list_types(category or None)]
        )

    @mcp.tool()
    def get_step_schema(
        step_type: Annotated[str, Field(
            description='Single step type name (e.g. "workflow_send_email").'
        )] = "",
        step_types: Annotated[list[str], Field(
            description="Optional list of step type names to retrieve schemas for in a single batch call."
        )] = [],
    ) -> StepSchema:
        """
        Get the configurable fields for one or more workflow step types.

        Returns field names, types, allowed values and descriptions. Where a
        field takes a list of objects, item_fields shows what one item holds.
        Positioning fields are omitted — those are handled automatically. For
        multiple unfamiliar types, pass step_types=[...] to fetch every schema
        in one tool roundtrip.
        """
        requested = [str(item).strip() for item in (step_types or []) if str(item).strip()]
        if not requested and "," in str(step_type or ""):
            requested = [
                item.strip()
                for item in str(step_type).split(",")
                if item.strip()
            ]

        if requested:
            schemas = {item: _schema_for_step_type(item) for item in requested}
            return StepSchema(schemas=schemas)

        return _schema_for_step_type(step_type)
