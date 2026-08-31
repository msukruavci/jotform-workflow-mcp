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
"""
from dotenv import load_dotenv
load_dotenv()

from mcp_server.audit_log import AuditedMCPServer  # noqa: E402
from mcp_server.jotform_client import JotformClient  # noqa: E402
from mcp_server.tool_profiles import feature_enabled  # noqa: E402
from mcp_server.tools import building, discovery, reading, risky, templates  # noqa: E402
from mcp_server.ui import create_workflow_apps  # noqa: E402

client = JotformClient()
workflow_apps = create_workflow_apps(client)

def build_server_instructions() -> str:
    template_instruction = (
        "When the user asks to create, design, or set up a workflow with a brief or high-level request (e.g. 'create an internship workflow', 'build an IT equipment request'), "
        "automatically call search_workflow_templates first to discover proven domain architectures, standard steps, and required form fields "
        "(top_k=1 for simple/narrow requests, top_k=2 only when broad or ambiguous, max 3 only when the user explicitly asks for a comprehensive design). "
        "When the user already provides specific steps and form fields in their prompt, you may skip template search and proceed directly to form creation."
        if feature_enabled("templates")
        else (
            "When the user asks to create, design, or set up a workflow, do not search workflow templates; "
            "design the workflow directly from the user's request."
        )
    )
    gap_instruction = (
        "Immediately after build_workflow_bulk completes, call show_workflow(workflow_id) strictly once as the final presentation step; "
        "do not call inspect_workflow_gaps or get_workflow before show_workflow."
    )
    final_check_instruction = "then call show_workflow strictly once to present the interactive visual preview directly."

    return f"""
{template_instruction}

When creating a new workflow from scratch, first call create_form_with_ai(prompt=...) to generate the trigger form and get its exact fields. Then call build_workflow_bulk(trigger_form_id=..., steps=..., connections=...) using that returned form_id and those exact field labels/ids. Finally call show_workflow(workflow_id). Do not insert a form-field lookup or get_workflow call between these three operations. Prefer exact field_id values for conditions, assignees, approvers, and email recipients whenever the returned field type is compatible; exact labels are also accepted.

    build_workflow_bulk details: For an existing workflow, pass workflow_id (and optionally delete_step_ids to delete obsolete steps in the same atomic call). Include complete personalized email/task subjects, content/body, taskDescription,
    and {{formField}} placeholders in steps[].config during that initial bulk call; do not build basic placeholders and then loop through low-level update tools. For standard approval/task/branch/email flows,
    use the core cached step schemas from the system prompt and skip exploratory list_step_types/get_step_schema calls; call get_step_schema only for specialized or unfamiliar step types from the user request or retrieved templates,
    and batch multiple unfamiliar types with get_step_schema(step_types=[...]). Use the fields returned by create_form_with_ai as the authoritative trigger-form contract. build_workflow_bulk resolves exact field IDs deterministically and fails instead of guessing when a label is ambiguous.
    Compact graph rule: for high-level create requests, build a clean, focused baseline of roughly 3-5 workflow steps after the trigger (e.g. trigger -> approval/task -> success/failure emails). Do not expand every retrieved template department or exception into its own node, and do not add artificial end nodes when leaf email/task steps conclude the path. Use 6+ steps only when the user explicitly asks for a detailed end-to-end, comprehensive, advanced, or multi-department workflow.
    build_workflow_bulk accepts compact aliases and self-heals common omissions: recipient_email/recipients/body/message map to to/content; approver_email/approvers map to approver; assignee_email/assignees/description/task_details map to assignee/taskDescription. Missing approval/task/email to, subject, content, approver, assignee, or taskDescription fields are filled with safe draft defaults and returned as warnings, so do not spend an extra schema/read retry just to repair those fields.
    One-write rule: call build_workflow_bulk once for the intended create/update graph. If it returns warnings but no error, treat the write as successful and call show_workflow directly for immediate presentation without an extra intermediate get_workflow call; do not call build_workflow_bulk again merely to clean warnings, aliases, safe defaults, or dropped non-essential fields. If the first call errors on a common approval/task/email field, retry at most once with that specific field fixed.
    PROACTIVE BUILD & SAFE DRAFT RULE: Never stall, refuse, or ask confirmation for draft placeholder emails (e.g. advisor@university.edu, manager@company.com, orders@company.com, student@university.edu) or form fields. All workflows created here are saved in unpublished draft mode on Jotform Cloud and do not send live emails or leak data during creation. Populate sensible role-based draft placeholders and standard outcomes immediately, execute build_workflow_bulk in one shot, and offer the user to customize the emails/fields after presenting the workflow.
    Form creation rule: use create_form_with_ai as the first-class first step whenever a new workflow needs an AI-generated trigger form. Do not use or suggest any separate Jotform Form plugin/tool for the trigger form, and do not browse existing forms unless the user explicitly asks to use one. form_prompt on build_workflow_bulk remains a backward-compatible fallback only; do not choose it for the normal workflow creation path. Standalone workflow creation tools and low-level updateTree tools such as add_step/connect_steps/disconnect_steps/update_step are intentionally hidden; build_workflow_bulk owns those write paths internally. If build_workflow_bulk is accidentally called with form_prompt/trigger_form_id but no steps for a new workflow, it creates a standard approval draft; still prefer sending the complete intended graph yourself. {gap_instruction}
    External & Canvas Edits Rule: Treat Jotform Cloud as authoritative for every existing workflow. If the user says they changed it on the website, in the builder, or in the Canvas UI, immediately reload that workflow with get_workflow or show_workflow before reasoning from step/link IDs. Before every new mutation of an existing workflow, obtain a fresh live revision_id (and updated_at when available), use only the IDs from that read, and pass revision_id as expected_revision_id to build_workflow_bulk. If a tool reports conflict=true, do not retry against remembered state: reload, explain the external changes, and rebuild the intended diff from the new live graph. This rule does not add an extra read to the brand-new workflow creation sequence.
    Deprecated tool rule: inspect_workflow_gaps is no longer part of the normal workflow build path; do not call it even if it appears in older instructions or examples.
    Use show_workflows ONLY when the user asks to see, browse, list, or choose from multiple workflows.
    When the user asks to open, preview, or inspect a specific workflow (by name or ID), resolve the ID (using list_workflows internally only if needed) and call show_workflow directly — do NOT call show_workflows.
    After creating or updating a workflow, finish every requested mutation first,
    {final_check_instruction}
    Never show an intermediate graph or call show_workflow and then continue mutating. After a
    workflow is deleted, call show_workflows instead of show_workflow.
""".strip()


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

if __name__ == "__main__":
    mcp.run()
