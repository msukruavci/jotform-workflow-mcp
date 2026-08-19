"""
Jotform Workflow MCP server.

Run locally (stdio):   python -m mcp_server.server

Tool layers:
  1. discovery — list_step_types, get_step_schema
  2. templates — search_workflow_templates
  3. reading   — list_workflows, get_workflow, get_step_details,
                 inspect_workflow_gaps, list_forms, get_form_fields
  4. building  — create_workflow, create_workflow_with_ai_form, build_workflow_bulk,
                 add_step, connect_steps, disconnect_steps, update_step
  5. risky     — delete_step, publish_workflow, restore_workflow_revision,
                 delete_workflow (confirm=True required to act)
"""
from dotenv import load_dotenv
load_dotenv()

from mcp_server.audit_log import AuditedMCPServer  # noqa: E402
from mcp_server.jotform_client import JotformClient  # noqa: E402
from mcp_server.tools import building, discovery, reading, risky, templates  # noqa: E402
from mcp_server.ui import create_workflow_apps  # noqa: E402

client = JotformClient()
workflow_apps = create_workflow_apps(client)

SERVER_INSTRUCTIONS = """
When the user asks to create, design, or set up a workflow (even with brief, high-level requests), automatically call search_workflow_templates first to discover proven architectures. Proactively create the workflow with AI trigger form and standard steps, inspect health/gaps, and call show_workflow to present the complete visual workflow.
Use show_workflows ONLY when the user asks to see, browse, list, or choose from multiple workflows.
When the user asks to open, preview, or inspect a specific workflow (by name or ID), resolve the ID (using list_workflows internally only if needed) and call show_workflow directly — do NOT call show_workflows.
After creating or updating a workflow, finish every requested mutation first,
read back the final authoritative state, inspect gaps when claiming completion,
then call show_workflow exactly once. Never show an intermediate graph. After a
workflow is deleted, call show_workflows instead of show_workflow.
""".strip()

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
