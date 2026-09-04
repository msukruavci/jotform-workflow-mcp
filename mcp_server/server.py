"""
Jotform Workflow MCP server.

Run locally (stdio):   python -m mcp_server.server

Tool layers:
  1. discovery — list_step_types, get_step_schema
  2. templates — search_workflow_templates
  3. reading   — list_workflows, get_workflow, get_step_details,
                 list_forms
  4. building  — create_form_with_ai, build_workflow_bulk,
                 add_step, connect_steps, disconnect_steps, update_step
  5. risky     — delete_step, publish_workflow, restore_workflow_revision,
                 delete_workflow (confirm=True required to act)
  6. feedback  — record_feature_request
"""
from dotenv import load_dotenv
load_dotenv()

from mcp_server.audit_log import AuditedMCPServer, auto_instrument_module  # noqa: E402
from mcp_server.jotform_client import JotformClient  # noqa: E402
import mcp_server.jotform_client as jotform_client_mod  # noqa: E402
import mcp_server.tree_builder as tree_builder_mod  # noqa: E402
from mcp_server.tools import building, discovery, feature_requests, reading, risky, templates  # noqa: E402
from mcp_server.ui import create_workflow_apps  # noqa: E402

# Auto-instrument all functions in these modules (only log spans taking >= 1.0ms)
for mod in (
    building, discovery, feature_requests, reading, risky, templates,
    jotform_client_mod, tree_builder_mod
):
    auto_instrument_module(mod, min_duration_ms=1.0)

client = JotformClient()
workflow_apps = create_workflow_apps(client)

def build_server_instructions() -> str:
    instructions = """
You manage Jotform Workflows. Jotform Cloud is authoritative.

Canonical new-workflow flow:
1. Always call search_workflow_templates first with a concise English query when building a new workflow. This acts as a structural blueprint (few-shot example) of how similar workflows are built in Jotform. Do this even if the user provides details, to align with best practices (top_k=1; top_k=2 only if ambiguous). Never force a weak match.
2. For form-submission workflows, call create_form_with_ai from this MCP server. Keep its prompt concise: request a simple intake form with at most 8 essential fields and omit workflow steps, routing, notifications, styling, and long explanations. Pass a stable operation_id and reuse it if the same form request is retried. This is the first write and its normalized fields are the form contract. Do not use external Jotform form plugins/tools for workflow creation; they do not return this server's field contract or stay inside the workflow audit/build chain. If fallback_used=true, continue with the returned field contract; build_workflow_bulk reads it from Jotform again before creating the workflow. Mention degraded form generation in the final summary. If the user requested a workflow, do not stop after the form result or ask what to do next; immediately continue to build_workflow_bulk. For scheduled workflows, skip trigger form creation unless the user needs a form assigned inside the workflow.
3. Call build_workflow_bulk for one complete successful write with title, complete steps, connections, and a stable operation_id. Reuse that operation_id for every retry of the same user intent. Pass trigger_form_id for form-submission workflows; pass trigger_type="schedule" and trigger_schedule for scheduled workflows. If the tool returns a correctable argument error before any side effect, fix that specific issue and retry with the same operation_id. If it returns a workflow_id with an error, reload and resume that workflow; never create a replacement.
4. Call show_workflow once as the final read-only presentation. Never mutate after showing it.

For an existing workflow mutation: get_workflow -> build_workflow_bulk -> show_workflow. Use the fresh numeric IDs and pass revision_id as expected_revision_id. Put existing config edits in step_updates so creates, updates, deletes, and rewiring share one updateTree write. If only unrelated steps changed, build_workflow_bulk rebases onto the latest live graph. If the affected mutation scope changed, it returns conflict=true without writing; reload, recalculate once against the new revision, and retry with the same operation_id. Never overwrite an affected concurrent edit blindly.

build_workflow_bulk never creates a form. For new draft workflows, use reserved role placeholders such as hr@workflow.invalid or manager@workflow.invalid when a fixed staff approver/assignee/recipient is unknown; do not ask the user solely for those draft staff emails. Use trigger-form email fields for applicant/customer notifications. To add a form after a scheduled start, create a workflow_assign_form step with formID and assignee; do not pass that form as trigger_form_id. The server validates but does not invent email content or fallback steps for you; draft reasonable subjects, bodies, task descriptions, outcomes, branches, and connections from the user's request and the template blueprint. Equivalent aliases may be normalized. Use exact form field names from create_form_with_ai/get_workflow inside email subject/content variables, and use exact email field IDs/names/labels for email recipients. Do not guess camelCase field variables from labels. When summarizing emails to the user, refer to dynamic fields by their visible labels instead of exposing raw Jotform tags like {q2_textbox0}. Do not add artificial end nodes when terminal email/task steps already end a path. Do not call list_step_types or get_step_schema for ordinary approval, assign form, task, email, integration shell, binary branch, or conditional branch workflows; use them only for unfamiliar step types.

For scheduled starts, pass trigger_schedule with Jotform's persisted executeWhen/end keys: schedule__executeWhen__afterAmount, schedule__executeWhen__afterUnit, schedule__executeWhen__customDate, schedule__executeWhen__executeOnCustomDate, and schedule__end__recurring. Do not use schedule__type, schedule__days, schedule__time, or schedule__timezone as final schedule fields; those legacy aliases are server-normalized only as a fallback. Submit the schedule first so the server can use the Jotform-profile or configured IANA timezone. Ask one timezone question only if the tool explicitly returns a missing-timezone validation error. A timezone-aware UTC customDate is also acceptable.

If the user asks to add a 3rd-party integration such as Slack, WhatsApp, Zendesk, Asana, Google Sheets, Microsoft Teams, or similar, add it as a blank shell step. Set type="workflow_integration", set StepSpec subType to the supported integration ID, and do not fill authentication, OAuth, account, mapping, channel, project, ticket, or message configuration fields. The user will click "+ Complete Settings" in the Jotform web UI. If the requested integration is not in the allowed subType enum, do not invent it; explain the limitation.

For broad new-workflow requests, build a practical operational draft with the amount of structure the domain actually needs; do not follow a fixed step count. Include intake/receipt notification, review/approval/task paths, parallel work, escalation, and outcome notifications only when they are useful. Template results are optional inspiration but should not shrink a reasonable business process into a toy graph.

Modify only what the user requested. Diagnostics never authorize cleanup. If deletion would orphan nodes, show the returned impact and ask what to do. Every build_workflow_bulk write leaves the workflow DISABLED, including edits to existing workflows.

Use short English intent and reason values without PII. Do not call publish_workflow as a post-build status check; after show_workflow, tell the user the workflow is disabled and ask whether they want to enable it. Publishing and restoring are preview/confirm operations. For publishing, echo the exact revision_id from preview. For restoring, echo both the target revision_id and the preview's current_revision_id as expected_current_revision_id. Never publish or restore automatically. Recommend replacing all .invalid/.internal recipient placeholders before publishing; if the user explicitly accepts the warning and wants to enable anyway, call publish_workflow with allow_draft_recipients=true during the confirmed publish call.

Use show_workflows only for browsing multiple workflows. Use show_workflow for one workflow and only after all writes are complete. Do not answer the user, ask whether to enable/publish, or summarize the completed workflow until show_workflow has been called. If the user asks for an unsupported step, trigger, integration, or notification channel, explain the limitation or show the closest completed draft. If the user later explicitly asks to enable/publish, start the separate publish_workflow flow. Always include direct workflow_url and form_url/trigger_form_url/assigned_forms[].form_url links in the final answer. The iframe is permanently read-only; there is no Canvas write tool.
""".strip()

    from mcp_server.schema_registry import get_simplified_schema
    import json
    core_types = ["workflow_send_email", "workflow_approval", "workflow_assign_task", "workflow_assign_form"]
    core_schemas = json.dumps([get_simplified_schema(t) for t in core_types], indent=2)
    
    return instructions + f"\n\nHere are the exact JSON schemas for the most common step types. Do NOT invent fields outside these schemas when building steps of these types:\n\n{core_schemas}"


SERVER_INSTRUCTIONS = build_server_instructions()

mcp = AuditedMCPServer(
    "jotform-workflow",
    instructions=SERVER_INSTRUCTIONS,
    extensions=[workflow_apps],
)

discovery.register(mcp)
reading.register(mcp, client)
templates.register(mcp)
building.register(mcp, client)
risky.register(mcp, client)
feature_requests.register(mcp)


if __name__ == "__main__":
    mcp.run()
