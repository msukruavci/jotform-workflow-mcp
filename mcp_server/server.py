"""
Jotform Workflow MCP server.

Run locally (stdio):   python -m mcp_server.server

Tool layers:
  1. discovery — list_step_types, get_step_schema
  2. reading   — list_workflows, get_workflow, get_step_details,
                 list_forms, get_form_fields
  3. building  — create_workflow, add_step, connect_steps, update_step
  4. risky     — delete_step, publish_workflow (confirm=True required to act)
"""
from dotenv import load_dotenv
from mcp.server import MCPServer

load_dotenv()

from mcp_server.jotform_client import JotformClient  # noqa: E402
from mcp_server.tools import building, discovery, reading, risky  # noqa: E402

mcp = MCPServer("jotform-workflow")
client = JotformClient()

discovery.register(mcp)
reading.register(mcp, client)
building.register(mcp, client)
risky.register(mcp, client)

if __name__ == "__main__":
    mcp.run()