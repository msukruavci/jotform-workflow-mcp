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

from typing import Literal

from pydantic import BaseModel, Field

from mcp_server.integrations import SupportedWorkflowIntegrationSubType


class WorkflowSummary(BaseModel):
    workflow_id: str | None = None
    workflow_url: str | None = None
    title: str | None = None
    status: str | None = Field(None, description="ENABLED / DISABLED etc.")
    updated_at: str | None = None
    run_count: str | int | None = Field(
        None, description="How many times this workflow has run"
    )


class WorkflowList(BaseModel):
    workflows: list[WorkflowSummary] = Field(default_factory=list)
    limit: int = 50
    offset: int = 0
    count: int = 0
    has_more: bool = False
    next_offset: int | None = None
    error: str | None = None


class Step(BaseModel):
    step_id: str | None = None
    type: str | None = None
    label: str | None = Field(None, description="Human label; defaults from type")
    trigger_form_id: str | None = None
    trigger_form_url: str | None = None
    sign_url: str | None = None
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
            "step, the branch name on a conditional branch, or the outcome "
            "button text on an approval/task. None means the step it leaves "
            "does not branch (a split's paths are equivalent, so they carry no label)."
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


class EmailStepIncoming(BaseModel):
    link_id: str | None = None
    from_step: str | None = None
    outcome: str | None = None


class EmailStepSummary(BaseModel):
    step_id: str | None = None
    label: str | None = None
    to: list[str] = Field(default_factory=list)
    subject: str | None = None
    content_present: bool = False
    content_excerpt: str | None = Field(
        None,
        description="Short plain-text email body preview, omitted when empty.",
    )
    missing_fields: list[str] = Field(
        default_factory=list,
        description=(
            "Required email fields that are empty in Jotform. If this is not empty, "
            "the step must not be treated as satisfying a requested email/survey/notification."
        ),
    )
    incoming: list[EmailStepIncoming] = Field(
        default_factory=list,
        description="How this email is reached; outcome identifies the approval/task branch.",
    )


class StepEdgeSummary(BaseModel):
    link_id: str | None = None
    step_id: str | None = None
    outcome: str | None = None


class StepStateSummary(BaseModel):
    step_id: str | None = None
    type: str | None = None
    label: str | None = None
    incoming: list[StepEdgeSummary] = Field(default_factory=list)
    outgoing: list[StepEdgeSummary] = Field(default_factory=list)
    key_config: dict = Field(
        default_factory=dict,
        description=(
            "Small deterministic config summary for the step type. Avoids raw "
            "Jotform UI metadata and exposes only fields useful for exact-match decisions."
        ),
    )
    missing_fields: list[str] = Field(
        default_factory=list,
        description=(
            "Important fields missing from this persisted step. If non-empty, "
            "do not treat the step as satisfying a user's requested action."
        ),
    )
    config_complete: bool = Field(
        True,
        description="False when missing_fields is non-empty.",
    )


class WorkflowHealth(BaseModel):
    """Internal/advisory graph diagnostics, never mutation instructions."""

    total_steps: int = 0
    unreachable_steps: list[str] = Field(
        default_factory=list,
        description="Advisory only: steps with no path from start; they may be intentional drafts",
    )
    dead_end_steps: list[str] = Field(
        default_factory=list,
        description="Advisory only: reached leaf steps; they may intentionally end the draft flow",
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
            "Advisory only: outcomes defined on a branching step but wired to nothing, e.g. "
            "an if/else whose FALSE path goes nowhere or a task outcome with "
            "no next step. Never connect or remove them without an explicit user request."
        ),
    )
    invalid_branch_links: list[str] = Field(
        default_factory=list,
        description=(
            "Outcome mappings whose linkID does not exist or leaves another step"
        ),
    )
    unlabelled_branching_steps: list[str] = Field(
        default_factory=list,
        description=(
            "Outgoing links from branching steps that are not mapped from any outcome"
        ),
    )


class WorkflowDetail(BaseModel):
    workflow_id: str | None = None
    workflow_url: str | None = None
    title: str | None = None
    status: str | None = None
    publish_status: str | None = None
    revision_id: str | None = Field(
        None,
        description="Live snapshot token to pass as expected_revision_id before a mutation.",
    )
    updated_at: str | None = None
    steps: list[Step] = Field(default_factory=list)
    connections: list[Connection] = Field(default_factory=list)
    step_states: list[StepStateSummary] = Field(
        default_factory=list,
        description=(
            "Generic exact-match state summary for all known workflow steps. "
            "Use this before claiming an externally edited step already satisfies a request."
        ),
    )
    email_steps: list[EmailStepSummary] = Field(
        default_factory=list,
        description=(
            "Compact exact-match summary for email steps: recipients, subject, "
            "whether body content exists, missing fields, and incoming branch path."
        ),
    )
    trigger_form_fields: list[FormField] = Field(
        default_factory=list,
        description=(
            "Fields/questions from the trigger form, included so callers do not "
            "need a separate field-inspection call after get_workflow."
        ),
    )
    health: WorkflowHealth | None = Field(
        default=None,
        exclude=True,
        description="Internal diagnostics excluded from normal model-facing reads.",
    )
    diagnostics: dict = Field(
        default_factory=dict,
        exclude=True,
        description="Internal notes excluded from normal model-facing reads.",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Read degradations that must not be interpreted as absent workflow data.",
    )
    error: str | None = None


class WorkflowListUIResult(BaseModel):
    """Versioned payload consumed by the workflow MCP UI list view."""

    view: Literal["workflow-list"] = "workflow-list"
    schema_version: Literal[1] = Field(1, alias="schemaVersion")
    data: WorkflowList


class WorkflowPreviewData(BaseModel):
    """Authoritative, native-canvas payload used only by the MCP UI."""

    workflow_id: str | None = None
    workflow_url: str | None = None
    title: str | None = None
    status: str | None = None
    publish_status: str | None = None
    settings_runtime_url: str | None = Field(
        default=None,
        description="Optional HTTPS UMD runtime used by the MCP settings host",
    )
    revision_id: str | None = Field(
        None,
        description="Live snapshot token for a later revision-checked bulk mutation.",
    )
    updated_at: str | None = None
    elements: list[dict] = Field(
        default_factory=list,
        description="Persisted Workflow element properties for the native read-only canvas",
    )
    links: list[dict] = Field(
        default_factory=list,
        description="Persisted Workflow links for the native read-only canvas",
    )
    step_states: list[StepStateSummary] = Field(
        default_factory=list,
        description=(
            "Generic exact-match state summary for all known workflow steps. "
            "Prefer this over raw elements when deciding whether a requested action already exists."
        ),
    )
    email_steps: list[EmailStepSummary] = Field(
        default_factory=list,
        description=(
            "Compact exact-match summary for email steps. Prefer this over raw "
            "elements when deciding whether a requested email already exists."
        ),
    )
    known_element_ids: list[str] = Field(
        default_factory=list,
        description="Element ids whose types are supported by this server and renderer",
    )
    health: WorkflowHealth | None = Field(
        default=None,
        exclude=True,
        description="Internal diagnostics excluded from normal model-facing previews.",
    )
    diagnostics: dict = Field(
        default_factory=dict,
        exclude=True,
        description="Internal notes excluded from normal model-facing previews.",
    )
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None


class WorkflowPreviewUIResult(BaseModel):
    """Versioned payload consumed by the workflow MCP UI preview."""

    view: Literal["workflow-preview"] = "workflow-preview"
    schema_version: Literal[2] = Field(2, alias="schemaVersion")
    data: WorkflowPreviewData


class WorkflowRevisionSummary(BaseModel):
    revision_id: str | None = None
    timestamp: str | None = None
    session_id: str | None = None
    workflow_id: str | None = None
    workflow_url: str | None = None
    reason: str | None = None
    title: str | None = None
    step_count: int = 0
    link_count: int = 0
    remote_revision_id: str | None = None
    remote_updated_at: str | None = None


class WorkflowRevisionList(BaseModel):
    workflow_id: str | None = None
    workflow_url: str | None = None
    revisions: list[WorkflowRevisionSummary] = Field(default_factory=list)
    error: str | None = None


class WorkflowGap(BaseModel):
    severity: str = Field(description="error / warning / info")
    category: str
    step_id: str | None = None
    step_type: str | None = None
    field: str | None = None
    message: str
    suggested_question: str | None = None


class WorkflowGapReport(BaseModel):
    workflow_id: str | None = None
    workflow_url: str | None = None
    trigger_form_id: str | None = None
    trigger_form_url: str | None = None
    ok_to_publish: bool = False
    issues: list[WorkflowGap] = Field(default_factory=list)
    available_form_fields: list[FormField] = Field(default_factory=list)
    error: str | None = None


class StepDetail(BaseModel):
    step_id: str | None = None
    type: str | None = None
    sign_url: str | None = None
    config: dict = Field(default_factory=dict, description="Full step configuration")
    error: str | None = None


class FormSummary(BaseModel):
    form_id: str | None = None
    form_url: str | None = None
    title: str | None = None
    status: str | None = None
    submission_count: str | int | None = None


class FormList(BaseModel):
    forms: list[FormSummary] = Field(default_factory=list)
    limit: int = 50
    offset: int = 0
    count: int = 0
    has_more: bool = False
    next_offset: int | None = None
    error: str | None = None


class FormField(BaseModel):
    field_id: str | None = Field(
        None, description="Exact Jotform question ID used in workflow field references."
    )
    name: str | None = Field(
        None,
        description=(
            "Exact Jotform unique question name used inside dynamic variables, "
            "for example {q3_email1}. Prefer this over guessing camelCase labels."
        ),
    )
    label: str | None = Field(
        None, description="Exact visible form-field label."
    )
    type: str | None = Field(
        None, description="Exact Jotform question/control type."
    )
    required: str | bool | None = Field(
        None, description="Whether the form field is required, as returned by Jotform."
    )
    options: list[str] = Field(
        default_factory=list,
        description="Exact allowed option texts for dropdown/radio/choice fields",
    )


class FormFieldList(BaseModel):
    form_id: str | None = None
    form_url: str | None = None
    fields: list[FormField] = Field(default_factory=list)
    error: str | None = None


class StepTypeSummary(BaseModel):
    step_type: str
    category: str
    description: str
    ui_name: str | None = Field(
        None, description="What this step is called in the Jotform builder UI"
    )
    canonical_type: str | None = Field(
        None, description="Actual API element type used when this is a UI variant"
    )
    subtype: str | None = Field(
        None, description="Automatically applied subtype for a UI variant"
    )
    schema_available: bool = True


class StepTypeList(BaseModel):
    step_types: list[StepTypeSummary] = Field(default_factory=list)


class SchemaField(BaseModel):
    name: str
    type: str
    description: str | None = None
    fixed_value: str | int | float | bool | None = None
    allowed_values: list = Field(default_factory=list)
    item_fields: dict = Field(default_factory=dict)


class StepSchemaItem(BaseModel):
    step_type: str | None = None
    canonical_type: str | None = None
    subtype: str | None = None
    description: str | None = None
    ui_name: str | None = None
    fields: list[SchemaField] = Field(default_factory=list)


class StepSchema(StepSchemaItem):
    schemas: dict[str, StepSchemaItem] = Field(
        default_factory=dict,
        description="Batch result mapping step_type to its schema result",
    )
    error: str | None = None
    hint: str | None = None
    available_types: list[str] = Field(default_factory=list)


# --- Layer 3: building ------------------------------------------------

class CreateWorkflowResult(BaseModel):
    workflow_id: str | None = None
    workflow_url: str | None = None
    title: str | None = None
    trigger_form_id: str | None = None
    trigger_form_url: str | None = None
    status: str | None = Field(
        None,
        description="New workflows are created DISABLED until the user explicitly enables them.",
    )
    error: str | None = None


class CreateAIFormResult(BaseModel):
    form_id: str | None = Field(
        None,
        description=(
            "New form ID from this MCP workflow tool. For form-submission workflows, "
            "pass it to build_workflow_bulk as trigger_form_id. For scheduled workflows "
            "that assign a form after the schedule starts, pass it as "
            "workflow_assign_form.formID. Do not substitute an external Jotform form "
            "plugin result for workflow creation."
        ),
    )
    form_url: str | None = Field(None, description="Jotform builder URL for the new form.")
    title: str | None = Field(None, description="Generated form title.")
    generation_mode: Literal["copilot", "fallback"] | None = Field(
        None, description="Whether Copilot or the public API fallback created the form."
    )
    fallback_used: bool = Field(
        False, description="True when Copilot was unavailable and a simpler fallback form was created."
    )
    fallback_reason: str | None = Field(None, description="Why fallback generation was used.")
    verified: bool = Field(
        False, description="True when the created form fields were read back from Jotform."
    )
    summary: str | None = Field(
        None,
        description=(
            "AI form-generation summary. This does not complete a workflow request; "
            "continue with build_workflow_bulk when the user asked for a workflow."
        ),
    )
    fields: list[FormField] = Field(
        default_factory=list,
        description=(
            "Complete normalized field contract for the next build_workflow_bulk call: "
            "field_id, name, label, type, required, and options. Use name inside "
            "email/task dynamic variables. External Jotform form plugins do not "
            "provide this workflow-ready contract."
        ),
    )
    next_required_tool: str | None = Field(
        None,
        description=(
            "Usually build_workflow_bulk. Continue the requested workflow instead "
            "of asking the user what to do."
        ),
    )
    hint: str | None = Field(
        None,
        description=(
            "Concrete next-step guidance for completing the workflow after form creation."
        ),
    )
    error: str | None = None


class CreateWorkflowWithAIFormResult(BaseModel):
    workflow_id: str | None = None
    workflow_url: str | None = None
    title: str | None = None
    trigger_form_id: str | None = None
    trigger_form_url: str | None = None
    status: str | None = Field(
        None,
        description="New workflows are created DISABLED until the user explicitly enables them.",
    )
    form_title: str | None = None
    form_summary: str | None = None
    questions: dict = Field(default_factory=dict)
    fields: list[FormField] = Field(
        default_factory=list,
        description="Simplified fields/questions from the created trigger form.",
    )
    error: str | None = None


class AddStepResult(BaseModel):
    step_id: str | None = None
    type: str | None = None
    existing_step_id: str | None = Field(
        None, description="Existing similar step found when duplicate creation is refused"
    )
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


class DisconnectStepsResult(BaseModel):
    link_id: str | None = None
    from_step: str | None = Field(
        None, description="The step the removed link used to leave from"
    )
    outcome_cleared: str | None = Field(
        None,
        description=(
            "If from_step branches, the outcome whose link was cleared "
            "so it can be wired to something else with connect_steps. "
            "None if from_step doesn't branch."
        ),
    )
    disconnected: bool = False
    error: str | None = None


class UpdateStepResult(BaseModel):
    step_id: str | None = None
    config: dict | None = Field(
        None,
        description="Saved element config derived from the updateTree result.",
    )
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
    hint: str | None = None


class StepSpec(BaseModel):
    ref: str = Field(
        description="A unique temporary reference name for this step in the bulk request (e.g. 'approval_1', 'notify_mgr', 'reject_email')."
    )
    type: str = Field(
        description=(
            'From list_step_types, e.g. "workflow_approval", "workflow_send_email". '
            'If the user asks to add a 3rd-party integration such as Slack, '
            'WhatsApp, Zendesk, Asana, Google Sheets, or Microsoft Teams, set '
            'type to "workflow_integration" and create a blank shell step only.'
        )
    )
    subType: SupportedWorkflowIntegrationSubType | None = Field(
        default=None,
        description=(
            "Required when type is workflow_integration. Must be one of the "
            "supported integration IDs, for example slack, whatsapp, "
            "whatsapp-business, zendesk, asana, google-sheetsV2, microsoft-teams. "
            "For 3rd-party integrations, add only a blank shell step: set type "
            "to workflow_integration, set this subType, and do NOT fill any "
            "authentication, account, mapping, OAuth, trigger, channel, project, "
            "workspace, ticket, or message configuration fields. The user will "
            "click '+ Complete Settings' in the Jotform UI."
        ),
    )
    config: dict = Field(
        default_factory=dict,
        description=(
            "Fields for this step type. For common approval/task/email/branch steps, "
            "use the documented compact configs directly. Draft staff recipients should "
            "use reserved role placeholders such as 'hr@workflow.invalid' or "
            "'manager@workflow.invalid' when no real address is provided; applicant/customer "
            "notifications should use the exact trigger form email field variable tag. "
            "CRITICAL WARNING for dynamic variables (e.g. in email content or approval tasks): "
            "NEVER use the question title/label wrapped in braces like '{Employee Name}'. "
            "ALWAYS use the exact 'name' property (unique name) from the fields/questions list "
            "returned by create_form_with_ai or get_workflow, like '{employeeName}'. "
            "When summarizing email content back to the user, refer to those dynamic fields by "
            "their visible labels instead of exposing raw Jotform tags such as '{q2_textbox0}'. "
            "For condition terms in build_workflow_bulk, prefer the trigger form's visible "
            "field label. Known field_id/qid/name values are also accepted; the bulk tool "
            "resolves them after creating/reading the trigger form and refuses ambiguous "
            "labels instead of guessing."
            " For workflow_integration steps, leave config empty or provide only "
            "a display name; put the integration ID in subType and never include "
            "auth/config fields."
        )
    )


class StepUpdateSpec(BaseModel):
    step_id: str = Field(
        description="Existing numeric step_id from a fresh get_workflow result."
    )
    config: dict = Field(
        default_factory=dict,
        description="Fields to merge into the existing step configuration.",
    )


class ConnectionSpec(BaseModel):
    from_ref: str = Field(
        description=(
            "The source step's ref name (e.g. 'start', '1', or 'approval_1'). "
            "'start' or '1' refers to the trigger form start point. When updating "
            "an existing workflow, an existing Jotform step_id from get_workflow "
            "is also accepted."
        )
    )
    to_ref: str = Field(
        description=(
            "The target step's ref name (e.g. 'approval_1', 'notify_mgr'). When "
            "updating an existing workflow, an existing Jotform step_id from "
            "get_workflow is also accepted."
        )
    )
    outcome: str = Field(
        default="",
        description=(
            "Required if from_ref is a branching step: if/else, conditional branch, "
            "approval, or task with outcomes (e.g. 'Approve', 'Deny', 'TRUE', 'FALSE'). "
            "Leave empty for non-branching steps."
        )
    )


class BuildWorkflowBulkResult(BaseModel):
    workflow_id: str | None = None
    workflow_url: str | None = None
    status: str | None = Field(
        None,
        description="Live workflow status. Every build_workflow_bulk write leaves the workflow DISABLED.",
    )
    trigger_form_id: str | None = None
    trigger_form_url: str | None = None
    created_steps: dict[str, str] = Field(
        default_factory=dict,
        description="Mapping from step ref to created Jotform step_id"
    )
    assigned_forms: list[dict] = Field(
        default_factory=list,
        description=(
            "Forms assigned by workflow_assign_form steps. Include these form_url links "
            "in the final answer, especially for scheduled workflows where the form is "
            "not the trigger_form_id."
        ),
    )
    updated_steps: list[str] = Field(default_factory=list)
    deleted_steps: list[str] = Field(
        default_factory=list,
        description="List of step IDs that were deleted in this bulk update",
    )
    deleted_links: list[str] = Field(default_factory=list)
    needs_confirmation: bool = Field(
        False,
        description=(
            "True when a requested destructive update was previewed but not applied "
            "because it needs an explicit user choice."
        ),
    )
    orphaned_step_ids: list[str] = Field(
        default_factory=list,
        description="Downstream step IDs that would become unreachable if the delete is applied.",
    )
    orphaned_steps: list[dict] = Field(
        default_factory=list,
        description="Labels/types for downstream steps that would become unreachable.",
    )
    delete_impacts: list[dict] = Field(
        default_factory=list,
        description=(
            "Topology preview for requested step deletes: incoming parents, outgoing "
            "children, reconnect candidates, and suggested user choices."
        ),
    )
    created_links_count: int = 0
    verified: bool = Field(
        False,
        description="True after the persisted workflow graph passes read-back verification.",
    )
    revision_id: str | None = None
    updated_at: str | None = None
    conflict: bool = False
    current_revision_id: str | None = None
    current_updated_at: str | None = None
    warnings: list[str] = Field(default_factory=list)
    next_required_tool: str | None = Field(
        "show_workflow",
        description="Call show_workflow immediately after this tool to present the visual canvas to the user."
    )
    error: str | None = None
    hint: str | None = None


# --- Layer 4: risky ------------------------------------------------------
# Risky tools carry needs_confirmation. publish_workflow previews first, then
# enables only after explicit confirmation and reports advisory warnings.

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
    verified: bool = False
    error: str | None = None
    hint: str | None = None


class PublishWorkflowResult(BaseModel):
    workflow_id: str | None = None
    workflow_url: str | None = None
    current_status: str | None = None
    target_status: str = "ENABLED"
    revision_id: str | None = Field(
        None, description="Live revision that must be echoed as expected_revision_id when confirming."
    )
    needs_confirmation: bool = False
    health_warnings: list[str] = Field(
        default_factory=list,
        description="Structural problems in the workflow as it stands — "
                     "reported alongside the publish result"
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


class RestoreWorkflowRevisionResult(BaseModel):
    workflow_id: str | None = None
    workflow_url: str | None = None
    revision_id: str | None = None
    revision_timestamp: str | None = None
    session_id: str | None = None
    reason: str | None = None
    target_title: str | None = None
    target_step_count: int = 0
    target_link_count: int = 0
    current_backup_revision_id: str | None = None
    current_revision_id: str | None = Field(
        None,
        description="Live workflow revision that the restore preview is bound to.",
    )
    needs_confirmation: bool = False
    restored: bool = False
    error: str | None = None
    hint: str | None = None
