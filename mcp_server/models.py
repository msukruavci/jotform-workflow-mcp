"""
Return shapes for the tools.

Why these exist: a tool annotated `-> dict` gives the SDK nothing to build
an outputSchema from, so the client receives a blob of JSON text instead of
structured data. Declaring the shape here means every tool ships a schema,
and it also pins down the contract — if a Jotform field name changes,
validation fails loudly instead of quietly producing `None`.

Every model carries an optional `error`. Tools return errors as data rather
than raising, because a raised exception tells the model nothing; an error
field it can read and explain to the user does.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class WorkflowSummary(BaseModel):
    workflow_id: str | None = None
    title: str | None = None
    status: str | None = Field(None, description="ENABLED / DISABLED etc.")
    updated_at: str | None = None
    run_count: str | int | None = Field(
        None, description="How many times this workflow has run"
    )


class WorkflowList(BaseModel):
    workflows: list[WorkflowSummary] = []
    error: str | None = None


class Step(BaseModel):
    step_id: str | None = None
    type: str | None = None
    label: str | None = Field(None, description="Human label; defaults from type")
    trigger_form_id: str | None = None
    known_type: bool = Field(
        True, description="False if this type has no schema on record"
    )


class Connection(BaseModel):
    link_id: str | None = None
    from_step: str | None = None
    to_step: str | None = None
    outcome: str | None = Field(
        None,
        description=(
            "Which branch this connection is — TRUE or FALSE on an if/else "
            "step, or the branch name on a conditional branch. None means the "
            "step it leaves does not branch (a split's paths are equivalent, "
            "so they carry no label)."
        ),
    )
    from_port: str | None = Field(
        None,
        description=(
            "Canvas exit port. Carries no meaning about which branch this is — "
            "it is layout, not logic. Kept because writing a link back requires "
            "it."
        ),
    )


class WorkflowHealth(BaseModel):
    total_steps: int = 0
    unreachable_steps: list[str] = Field(
        default_factory=list,
        description="Steps with no path from the start point — they never run",
    )
    dead_end_steps: list[str] = Field(
        default_factory=list,
        description="Steps that are reached but lead nowhere and aren't an end point",
    )
    unknown_types: list[str] = Field(
        default_factory=list, description="Step types with no schema on record"
    )
    dangling_links: list[str] = Field(
        default_factory=list,
        description="Links pointing at a step that no longer exists"
    )
    unconnected_branches: list[str] = Field(
        default_factory=list,
        description=(
            "Branches defined on a step but wired to nothing, e.g. an if/else "
            "whose FALSE path goes nowhere — described as 'step 2 FALSE'"
        ),
    )


class WorkflowDetail(BaseModel):
    workflow_id: str | None = None
    title: str | None = None
    status: str | None = None
    publish_status: str | None = None
    steps: list[Step] = []
    connections: list[Connection] = []
    health: WorkflowHealth | None = None
    diagnostics: dict = Field(
        default_factory=dict,
        description="Internal notes — e.g. link fields we could not interpret",
    )
    error: str | None = None


class StepDetail(BaseModel):
    step_id: str | None = None
    type: str | None = None
    config: dict = Field(default_factory=dict, description="Full step configuration")
    error: str | None = None


class FormSummary(BaseModel):
    form_id: str | None = None
    title: str | None = None
    status: str | None = None
    submission_count: str | int | None = None


class FormList(BaseModel):
    forms: list[FormSummary] = []
    error: str | None = None


class FormField(BaseModel):
    field_id: str | None = None
    label: str | None = None
    type: str | None = None
    required: str | bool | None = None


class FormFieldList(BaseModel):
    form_id: str | None = None
    fields: list[FormField] = []
    error: str | None = None


class StepTypeSummary(BaseModel):
    step_type: str
    category: str
    description: str
    ui_name: str | None = Field(
        None, description="What this step is called in the Jotform builder UI"
    )
    schema_available: bool = True


class StepTypeList(BaseModel):
    step_types: list[StepTypeSummary] = []


class SchemaField(BaseModel):
    name: str
    type: str
    description: str | None = None
    fixed_value: str | int | float | bool | None = None
    allowed_values: list = Field(default_factory=list)
    item_fields: dict = Field(default_factory=dict)


class StepSchema(BaseModel):
    step_type: str | None = None
    description: str | None = None
    ui_name: str | None = None
    fields: list[SchemaField] = []
    error: str | None = None
    hint: str | None = None
    available_types: list[str] = Field(default_factory=list)


# --- Layer 3: building ------------------------------------------------

class CreateWorkflowResult(BaseModel):
    workflow_id: str | None = None
    title: str | None = None
    trigger_form_id: str | None = None
    error: str | None = None


class AddStepResult(BaseModel):
    step_id: str | None = None
    type: str | None = None
    linked_from: str | None = Field(
        None, description="Step this was auto-connected from, if any"
    )
    warnings: list[str] = Field(
        default_factory=list, description="Config fields dropped or corrected"
    )
    error: str | None = None
    hint: str | None = None


class ConnectStepsResult(BaseModel):
    link_id: str | None = None
    from_step: str | None = None
    to_step: str | None = None
    outcome: str | None = None
    error: str | None = None
    hint: str | None = None


class UpdateStepResult(BaseModel):
    step_id: str | None = None
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None


# --- Layer 4: risky ------------------------------------------------------
# Every result here carries needs_confirmation. The pattern: call once
# without confirm=True to get a preview and nothing changes; call again
# with confirm=True — only after the model has shown the preview to the
# user and gotten an explicit yes — to actually act. Mirrors the confirm
# pattern this assistant itself uses for ending a conversation: the first
# call is a question, not an action.

class DeleteStepResult(BaseModel):
    step_id: str | None = None
    type: str | None = None
    label: str | None = None
    needs_confirmation: bool = False
    affected_connections: list[str] = Field(
        default_factory=list,
        description="Connections that will break — shown before acting"
    )
    deleted: bool = False
    error: str | None = None
    hint: str | None = None


class PublishWorkflowResult(BaseModel):
    workflow_id: str | None = None
    needs_confirmation: bool = False
    health_warnings: list[str] = Field(
        default_factory=list,
        description="Structural problems in the workflow as it stands — "
                     "shown before publishing, not a reason to block it"
    )
    published: bool = False
    error: str | None = None
    hint: str | None = None


class DeleteWorkflowResult(BaseModel):
    workflow_id: str | None = None
    title: str | None = None
    needs_confirmation: bool = False
    deleted: bool = False
    error: str | None = None
    hint: str | None = None