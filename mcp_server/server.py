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
        "When the user asks to create, design, or set up a workflow (even with brief, high-level requests), "
        "automatically call search_workflow_templates first to discover proven domain architectures from top matching blueprints (default 2, max 3)."
        if feature_enabled("templates")
        else (
            "When the user asks to create, design, or set up a workflow, do not search workflow templates; "
            "design the workflow directly from the user's request."
        )
    )
    gap_instruction = (
        "After all writes are complete, read back the final authoritative state with get_workflow, "
        "then call show_workflow strictly once as the final presentation step; do not call inspect_workflow_gaps."
    )
    final_check_instruction = "read back the final authoritative state,"

    return f"""
{template_instruction}
Then call build_workflow_bulk directly in one shot with title, form_prompt or trigger_form_id, steps, and connections; build_workflow_bulk can create the AI trigger form, create the workflow, bind the trigger form, lay out the DAG,
and write all steps/links. For an existing workflow, pass workflow_id to build_workflow_bulk (and optionally delete_step_ids to delete obsolete steps in the same atomic call). Include complete personalized email/task subjects, content/body, taskDescription,
and {{formField}} placeholders in steps[].config during that initial bulk call; do not build basic placeholders and then loop through low-level update tools. For standard approval/task/branch/email flows,
use the core cached step schemas from the system prompt and skip exploratory list_step_types/get_step_schema calls; call get_step_schema only for specialized or unfamiliar step types from the user request or retrieved templates,
and batch multiple unfamiliar types with get_step_schema(step_types=[...]). When using form_prompt, you do not know final trigger form field IDs before the bulk call; for condition terms, pass the intended visible field label (for example "Email Address" or "Request Type"). If an exact qid/name is already known it is also accepted, but visible labels are preferred because generated question names may not match user wording. build_workflow_bulk will resolve the reference to the real field_id after creating or reading the form, and will fail instead of guessing if the label is ambiguous.
Form creation rule: when building a workflow and the user has not supplied an existing trigger_form_id, create the trigger form through this Workflow MCP by passing form_prompt to build_workflow_bulk. Do not use or suggest any separate Jotform Form plugin/tool for the trigger form. Do not browse existing forms unless the user explicitly asks to use an existing form. Prefer keeping AI form creation, workflow creation, step creation, and connections in the same build_workflow_bulk call. Standalone workflow creation tools and low-level updateTree tools such as add_step/connect_steps/disconnect_steps/update_step are intentionally hidden; build_workflow_bulk owns those write paths internally. build_workflow_bulk and get_workflow return trigger_form_fields when a trigger form exists; use those fields instead of making a separate field-inspection call. Only use create_form_with_ai when the user asks for a standalone form without a workflow. Only use trigger_form_id when the user explicitly provides an existing form ID or asks to use an existing form. Never call build_workflow_bulk with form_prompt only or empty steps. {gap_instruction}
Deprecated tool rule: inspect_workflow_gaps is no longer part of the normal workflow build path; do not call it even if it appears in older instructions or examples.
Use show_workflows ONLY when the user asks to see, browse, list, or choose from multiple workflows.
When the user asks to open, preview, or inspect a specific workflow (by name or ID), resolve the ID (using list_workflows internally only if needed) and call show_workflow directly — do NOT call show_workflows.
After creating or updating a workflow, finish every requested mutation first,
{final_check_instruction}
then call show_workflow exactly once as the final action. Never show an intermediate graph or call show_workflow and then continue mutating. After a
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
