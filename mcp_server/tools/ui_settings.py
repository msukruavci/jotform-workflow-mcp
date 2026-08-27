"""Narrow settings mutations used by the embedded workflow UI.

The general ``update_step`` tool is intentionally flexible for agent-driven
workflow building. The embedded UI needs a smaller trust boundary: every
editable field is explicitly allow-listed here before it can reach Jotform.
"""
from __future__ import annotations

import re
from typing import Annotated

from mcp.server import MCPServer
from pydantic import Field

from mcp_server import revision_log, tree_builder as tb
from mcp_server.jotform_client import JotformAPIError, JotformClient
from mcp_server.models import UpdateStepSettingsResult


EDITABLE_FIELDS_BY_TYPE: dict[str, frozenset[str]] = {
    "workflow_send_email": frozenset({"name", "to", "subject", "content"}),
}

MAX_RECIPIENTS = 50
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
QUESTION_PATTERN = re.compile(r"^\{[^{}]+\}$")
RECIPIENT_FIELDS = frozenset({"text", "value", "isValid", "isQuestion"})


def _validate_recipients(value) -> tuple[list[dict], str | None]:
    if not isinstance(value, list):
        return [], "Recipients must be a list."
    if not value:
        return [], "Recipients cannot be empty."
    if len(value) > MAX_RECIPIENTS:
        return [], f"Recipients cannot contain more than {MAX_RECIPIENTS} entries."

    recipients: list[dict] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            return [], f"Recipient {index} must be an email or form field."

        unexpected_fields = set(item) - RECIPIENT_FIELDS
        if unexpected_fields:
            return [], f"Recipient {index} contains unsupported data."

        text = item.get("text")
        recipient_value = item.get("value")
        if not isinstance(text, str) or not text.strip():
            return [], f"Recipient {index} has no label."
        if recipient_value is not None and not isinstance(recipient_value, str):
            return [], f"Recipient {index} has an invalid value."
        if "isValid" in item and not isinstance(item["isValid"], bool):
            return [], f"Recipient {index} has an invalid validity flag."
        if "isQuestion" in item and not isinstance(item["isQuestion"], bool):
            return [], f"Recipient {index} has an invalid form-field flag."

        text = text.strip()
        recipient_value = (recipient_value or text).strip()
        is_question = item.get("isQuestion") is True

        if is_question:
            if not QUESTION_PATTERN.fullmatch(recipient_value):
                return [], f"Recipient {index} has an invalid form-field value."
            recipients.append({
                "text": text,
                "value": recipient_value,
                "isQuestion": True,
            })
            continue

        if item.get("isValid") is False or not EMAIL_PATTERN.fullmatch(recipient_value):
            return [], f"Recipient {index} is not a valid email address."
        recipients.append({
            "text": text,
            "value": recipient_value,
            "isValid": True,
        })

    return recipients, None


def _validate_changes(step_type: str, changes: dict) -> tuple[dict, str | None]:
    allowed_fields = EDITABLE_FIELDS_BY_TYPE.get(step_type)
    if allowed_fields is None:
        return {}, f"{step_type} cannot be edited in the MCP preview yet."

    if not isinstance(changes, dict) or not changes:
        return {}, "No settings changes were provided."

    blocked_fields = sorted(set(changes) - allowed_fields)
    if blocked_fields:
        return {}, (
            "These fields cannot be edited in the MCP preview: "
            + ", ".join(blocked_fields)
        )

    prepared_changes = dict(changes)
    if "to" in prepared_changes:
        recipients, recipient_error = _validate_recipients(prepared_changes["to"])
        if recipient_error:
            return {}, recipient_error
        prepared_changes["to"] = recipients

    for key, value in prepared_changes.items():
        if key != "to" and not isinstance(value, str):
            return {}, f"{key} must be text."

    if "name" in prepared_changes and not prepared_changes["name"].strip():
        return {}, "Step name cannot be empty."

    try:
        clean_changes, warnings = tb.validate_config(step_type, prepared_changes)
    except tb.ValidationError as error:
        return {}, str(error)

    if warnings or set(clean_changes) != set(prepared_changes):
        return {}, "One or more settings fields are not valid for this step."

    return clean_changes, None


def register(mcp: MCPServer, client: JotformClient) -> None:
    @mcp.tool()
    def update_step_settings(
        workflow_id: Annotated[str, Field(description="Workflow containing the step.")],
        step_id: Annotated[str, Field(description="Step being edited in the MCP UI.")],
        changes: Annotated[
            dict,
            Field(description="Only the UI fields changed by the user."),
        ],
    ) -> UpdateStepSettingsResult:
        """Update the small allow-listed set of fields exposed by the MCP UI."""
        try:
            current = client.get_element(workflow_id, step_id)
        except JotformAPIError as error:
            return UpdateStepSettingsResult(step_id=step_id, error=str(error))

        if not isinstance(current, dict):
            return UpdateStepSettingsResult(
                step_id=step_id,
                error="The server returned an invalid step configuration.",
            )

        step_type = current.get("type")
        if not isinstance(step_type, str) or not step_type:
            return UpdateStepSettingsResult(
                step_id=step_id,
                error="Could not determine this step's type.",
            )

        clean_changes, validation_error = _validate_changes(step_type, changes)
        if validation_error:
            return UpdateStepSettingsResult(
                step_id=step_id,
                type=step_type,
                config=current,
                error=validation_error,
                hint="Open this step in Jotform for settings that are not editable here.",
            )

        changed_fields = [
            key for key, value in clean_changes.items() if current.get(key) != value
        ]
        if not changed_fields:
            return UpdateStepSettingsResult(
                step_id=step_id,
                type=step_type,
                config=current,
            )

        update = {key: clean_changes[key] for key in changed_fields}
        try:
            revision_log.capture_workflow_revision(
                client,
                workflow_id,
                f"before update_step_settings {step_id}",
                tool_name="update_step_settings",
            )
            client.update_tree(
                workflow_id,
                elements=[tb.build_element_update(step_id, update)],
            )
        except JotformAPIError as error:
            return UpdateStepSettingsResult(
                step_id=step_id,
                type=step_type,
                config=current,
                error=str(error),
            )

        warnings: list[str] = []
        try:
            refreshed = client.get_element(workflow_id, step_id)
        except JotformAPIError:
            refreshed = {**current, **update}
            warnings.append(
                "The settings were saved, but the latest step could not be refreshed."
            )

        return UpdateStepSettingsResult(
            step_id=step_id,
            type=step_type,
            config=refreshed if isinstance(refreshed, dict) else {**current, **update},
            updated_fields=changed_fields,
            warnings=warnings,
        )
