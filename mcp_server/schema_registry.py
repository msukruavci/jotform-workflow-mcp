"""
Loads Jotform's official workflow element JSON Schemas and turns them
into something a language model can actually use.

Why simplify: the raw schemas are draft-07 JSON Schema with $refs,
regex patterns, anyOf/allOf branches and validation minutiae. A model
doesn't need any of that to decide "what fields can I send for an email
step" — it needs field names, types, allowed values, and a one-line
description. The full schema stays available for server-side validation,
which is where strictness actually matters.

Known limitation (2026-08-07): this schema file is behind the live
product. `workflow_payment_verification` exists in a real account but has
no schema here, and several builder-UI elements ("Approve & Sign",
"Team Approval", "Flow Report", "PDF") have no confirmed type mapping.
See docs/gap-report.md.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

SCHEMA_PATH = Path(__file__).parent / "schemas" / "workflow_all_schemas.json"

# Grouping keeps list_step_types readable. A flat list of 36 types with
# no structure is hard for a model to navigate; categories let it narrow
# down before pulling a full schema.
CATEGORIES = {
    "basic": [
        "workflow_start_point", "workflow_end_point", "workflow_send_email",
        "workflow_assign_task", "workflow_approval", "workflow_assign",
        "workflow_assign_form", "workflow_reminder_email",
    ],
    "logic": [
        "workflow_binary_decision", "workflow_conditional_branch",
        "workflow_split", "workflow_merge", "workflow_for_each",
        "workflow_pause", "workflow_collection",
    ],
    "ai": [
        "workflow_ai_generate_text", "workflow_ai_summarize_text",
        "workflow_ai_categorize", "workflow_ai_sentiment_analysis",
        "workflow_ai_extract_information", "workflow_ai_extract_from_file",
        "workflow_ai_custom_prompt", "workflow_ai_calculate",
        "workflow_ai_id_generator", "workflow_ai_task_automation",
        "workflow_ai_update_submission", "workflow_ai_response",
        "workflow_ai_agent_web_search",
    ],
    "integration": [
        "workflow_webhook", "workflow_integration", "workflow_payment_gateway",
        "workflow_payment_verification", "workflow_sign_document",
        "workflow_qr_code",
    ],
    "internal": [
        "workflow_placeholder", "workflow_ai_placeholder", "workflow_generic",
    ],
}

# Short, human descriptions. Jotform's own schema "title" fields are
# unreliable here — several AI step types are all titled "Webhook Schema",
# which would actively mislead a model choosing between them.
DESCRIPTIONS = {
    "workflow_start_point": "Entry point — the form submission that triggers the workflow",
    "workflow_end_point": "Marks the end of a branch",
    "workflow_send_email": "Send an email to one or more recipients",
    "workflow_assign_task": "Assign a task to someone, with a completion button",
    "workflow_approval": "Request approval — approver gets approve/deny options",
    "workflow_assign": "Assign the submission to a user",
    "workflow_assign_form": "Assign a form for someone to fill in",
    "workflow_reminder_email": "Send a reminder email after a delay",
    "workflow_binary_decision": "If/Else — two fixed outcomes (TRUE / FALSE)",
    "workflow_conditional_branch": "Conditional branching with named custom branches",
    "workflow_split": "Split the flow into multiple parallel paths",
    "workflow_merge": "Merge multiple paths back into one",
    "workflow_for_each": "Loop over a list of items",
    "workflow_pause": "Pause the flow for a set duration, or wait until a date",
    "workflow_collection": "Collect data from multiple sources",
    "workflow_ai_generate_text": "Generate text with AI (tone, length, style configurable)",
    "workflow_ai_summarize_text": "Summarize text with AI",
    "workflow_ai_categorize": "Categorize content with AI",
    "workflow_ai_sentiment_analysis": "Analyze sentiment of submitted text",
    "workflow_ai_extract_information": "Extract structured info from text with AI",
    "workflow_ai_extract_from_file": "Extract information from an uploaded file",
    "workflow_ai_custom_prompt": "Run a custom AI prompt",
    "workflow_ai_calculate": "Perform a calculation with AI",
    "workflow_ai_id_generator": "Generate an ID with AI",
    "workflow_ai_task_automation": "Automate a task with AI",
    "workflow_ai_update_submission": "Update the submission using AI output",
    "workflow_ai_response": "Produce an AI-generated response",
    "workflow_ai_agent_web_search": "Let an AI agent search the web",
    "workflow_webhook": "Call an external webhook URL",
    "workflow_integration": "Connect to a third-party service (e.g. Google Calendar)",
    "workflow_payment_gateway": "Take a payment",
    "workflow_payment_verification": "Verify a payment, with a verifier email",
    "workflow_sign_document": "Request a document signature (Jotform Sign)",
    "workflow_qr_code": "Generate a QR code",
    "workflow_placeholder": "Empty slot — created automatically, not a real step",
    "workflow_ai_placeholder": "Empty AI slot — created automatically",
    "workflow_generic": "Generic element",
}

# What each step is called in the Jotform builder's left panel. This exists
# because the user speaks the UI's language, not the API's: someone who has
# just closed the builder says "add an approval step", not
# "add a workflow_approval". Without this the model can't bridge the two.
#
# Confirmed from the builder UI (screenshot, 2026-08-07). Entries marked
# UNCONFIRMED are best guesses and must be verified before being trusted —
# see docs/gap-report.md.
UI_NAMES = {
    "workflow_assign_form": "Form",
    "workflow_send_email": "Email",
    "workflow_approval": "Approval",
    "workflow_assign_task": "Task",
    "workflow_sign_document": "Sign Document",
    "workflow_webhook": "Webhook",
    "workflow_qr_code": "QR Code",
    "workflow_ai_task_automation": "Task Automation",
    "workflow_payment_verification": "Payment Form",  # UNCONFIRMED
    "workflow_reminder_email": "Scheduled Email",     # UNCONFIRMED
    "workflow_binary_decision": "If/Else Condition",
    "workflow_conditional_branch": "Conditional Branch",
    "workflow_split": "Split Branches",
    "workflow_merge": "Merge Branches",
    "workflow_pause": "Wait / Pause",
    "workflow_start_point": "On Submission",
    "workflow_end_point": "End",
}

# Builder-UI elements we have seen but cannot map to any known type.
# Listed so the gap report has a concrete to-do rather than a vague
# "schemas may be incomplete".
UNMAPPED_UI_ELEMENTS = ["Team Approval", "Flow Report", "PDF"]

# "Approve & Sign" was in this list until 2026-08-11 — confirmed NOT
# unmapped. It's workflow_approval with subType "workflow_approval_with_sign",
# found by reading a real, working approval step's raw element data
# (probes/inspect_approval_outcomes.py), not guessed. A ChatGPT test session
# had independently guessed "workflow_approve_sign" for this — close, but
# wrong; the confirmed value differs by one character
# ("workflow_approval_with_sign"). Worth remembering: a subType string that
# merely sounds plausible is exactly the kind of thing this project avoids
# fabricating elsewhere (form field ids, emails) — the same discipline
# applies here, and this near-miss is why.
#
# "Team Approval"'s real subType is still unconfirmed — do not guess it.
# validate_config has no enum constraint on subType (it's a free string on
# every step schema that has one), so a wrong guess is silently accepted at
# creation time with no error — the same "accepted but not necessarily
# correct" pattern documented for link ports and setResource. It likely
# surfaces later as a mismatch between what the element claims to be and
# what real elements of that kind actually look like (plausibly the cause
# of the 404 seen wiring a guessed-subType "Team Approval" step's outcomes —
# unconfirmed without the same ground-truth check run against a real one).

# Step types where an outgoing link's meaning depends on which named outcome
# it fulfils (TRUE/FALSE, or a custom branch name) rather than just existing.
# Shared between reading.py (labelling connections) and tree_builder.py
# (deciding when connect_steps requires an `outcome` argument) — defined
# once here so the two can't drift apart.
BRANCHING_TYPES = {
    "workflow_binary_decision", "workflow_conditional_branch", "workflow_approval",
}

_raw_schemas: dict[str, Any] | None = None


def _load() -> dict[str, Any]:
    global _raw_schemas
    if _raw_schemas is None:
        with open(SCHEMA_PATH) as f:
            _raw_schemas = json.load(f)
    return _raw_schemas


def get_raw_schema(step_type: str) -> dict[str, Any] | None:
    """Full draft-07 schema — for server-side validation, not for the model."""
    return _load().get(step_type)


def is_known_type(step_type: str) -> bool:
    """True if we hold a schema for this type. Live workflows contain types
    we don't (see module docstring), so callers must handle False."""
    return step_type in _load()


def get_ui_name(step_type: str) -> str | None:
    return UI_NAMES.get(step_type)


def default_label(step_type: str | None) -> str:
    """
    Jotform leaves `name` empty on steps the user never renamed, so several
    steps come back with label=None. A workflow with three unnamed email
    steps is unreadable to a model. Fall back to the UI name.
    """
    if not step_type:
        return "Unnamed step"
    return UI_NAMES.get(step_type) or step_type.replace("workflow_", "").replace("_", " ").capitalize()


def _flatten_all_of(prop: dict) -> dict:
    """
    allOf branches hide both the $ref and the description. Jotform uses this
    for every rich field (`to`, `cc`, condition terms), so without flattening
    it the model sees type "any" with no description on exactly the fields
    that matter most. Measured: 14 fields collapsed to "any" before this.
    """
    if "allOf" not in prop:
        return prop
    merged: dict = {k: v for k, v in prop.items() if k != "allOf"}
    for branch in prop["allOf"]:
        if isinstance(branch, dict):
            for k, v in branch.items():
                merged.setdefault(k, v)
    return merged


def _simplify_property(name: str, prop: dict, definitions: dict) -> dict:
    prop = _flatten_all_of(prop)

    if "$ref" in prop:
        ref_name = prop["$ref"].split("/")[-1]
        resolved = definitions.get(ref_name, {})
        entry: dict[str, Any] = {
            "name": name,
            "type": resolved.get("type", "object"),
            "description": prop.get("description") or resolved.get("description", ""),
        }
        # For arrays of objects, show what one item looks like — otherwise the
        # model knows it must send a list, but not a list of what.
        items = resolved.get("items")
        if isinstance(items, dict) and isinstance(items.get("properties"), dict):
            entry["item_fields"] = {
                k: (v.get("type", "any") if isinstance(v, dict) else "any")
                for k, v in items["properties"].items()
                if k != "additionalProperties"
            }
        return entry

    prop_type = prop.get("type", "any")
    if isinstance(prop_type, list):
        prop_type = "|".join(prop_type)

    entry = {"name": name, "type": prop_type}

    if "const" in prop:
        entry["fixed_value"] = prop["const"]
    if "enum" in prop:
        entry["allowed_values"] = prop["enum"]
    # anyOf often hides the enum/const in a branch
    if "anyOf" in prop:
        options = []
        for branch in prop["anyOf"]:
            if not isinstance(branch, dict):
                continue
            if "const" in branch:
                options.append(branch["const"])
            elif "enum" in branch:
                options.extend(branch["enum"])
        if options:
            entry["allowed_values"] = options
    if prop.get("description"):
        entry["description"] = prop["description"]

    return entry


def get_simplified_schema(step_type: str) -> dict[str, Any] | None:
    """Model-facing view: field names, types, allowed values, descriptions."""
    schema = get_raw_schema(step_type)
    if schema is None:
        return None

    definitions = schema.get("definitions", {})
    properties = schema.get("properties", {})

    fields = [_simplify_property(n, p, definitions) for n, p in properties.items()]
    # Drop x/y — canvas positioning, handled server-side, and only distracts a
    # model that's trying to decide what a step should *do*.
    fields = [f for f in fields if f["name"] not in ("x", "y")]

    # `outcomes` on a handful of step types is declared in the raw schema
    # as a bare array with no `items` sub-schema — _simplify_property has
    # nothing to build item_fields from, so the model would see only
    # {"name": "outcomes", "type": "array"} and have to guess the object
    # shape to add a step with real branches. _OUTCOME_ITEM_FIELDS_OVERRIDE
    # below fills that in for step types where it's been confirmed against
    # a real, working element — never guessed.
    if step_type in _OUTCOME_ITEM_FIELDS_OVERRIDE:
        for f in fields:
            if f["name"] == "outcomes" and not f.get("item_fields"):
                f["item_fields"] = _OUTCOME_ITEM_FIELDS_OVERRIDE[step_type]

    return {
        "step_type": step_type,
        "description": DESCRIPTIONS.get(step_type, schema.get("title", "")),
        "ui_name": UI_NAMES.get(step_type),
        "fields": fields,
    }


def get_field_defaults(step_type: str) -> dict[str, Any]:
    """
    Every field on this step type with a schema-declared `default`,
    ready to inject into a create payload.

    Why this exists (2026-08-10): a workflow built entirely through this
    project opened to a blank canvas in Jotform's own builder — no error,
    just nothing rendered. Comparing our created elements against a real,
    working reference (probes/compare_element_shapes.py) showed the
    server does NOT auto-populate every field's schema default on create
    — most fields (subTypeText, content, fromName, ...) come back filled
    in regardless of what we sent, but a specific handful never do:
    workflow_send_email.to, workflow_binary_decision.conditionTerms,
    workflow_assign_task.assignee and .outcomes. Every one of these is an
    array the step's renderer iterates over — absent (not even an empty
    list), that iteration is believed to throw client-side, which a blank
    canvas with no error toast is consistent with (an uncaught render
    exception swallowed by a boundary, not a network failure).

    Rather than special-case exactly those four fields — which only
    protects against the one failure already found, not the same bug in
    a step type never diffed — this injects any field with a declared
    default that the caller didn't supply, for any step type. Redundant
    for fields the server was already defaulting anyway, which costs
    nothing; the fields that actually needed it are now covered by the
    same mechanism that already existed for branching `outcomes` (see
    docs/decision-log.md, 2026-08-10, "Inject default outcomes on
    branching-type create") — that fix was a special case of this
    general rule all along.

    `name` is deliberately excluded even though schemas declare a default
    for it ("Email", "Task", ...): the reference element above had no
    `name` key at all and rendered fine — a real element only carries one
    if its creator actually renamed it. Injecting a generic default would
    be a needless deviation from what a real Jotform element looks like.

    Second layer, added 2026-08-11: some step types have a real, working
    `outcomes` mechanism whose schema simply declares no `default` at all
    — workflow_approval confirmed via probes/inspect_approval_outcomes.py.
    A workflow_approval created without an explicit outcomes array has
    nothing for connect_steps to wire Approve/Deny to, same failure shape
    as the branching-type bug above, but the schema-reading approach can't
    fix it because there's no schema default to read. _OUTCOMES_OVERRIDE
    below holds hand-verified defaults for exactly these cases, sourced
    from a real element's actual data — never guessed — and only applies
    when the schema itself provided nothing.
    """
    raw = get_raw_schema(step_type) or {}
    defaults: dict[str, Any] = {}
    for name, prop in raw.get("properties", {}).items():
        if name in ("type", "x", "y", "name"):
            continue
        flat = _flatten_all_of(prop) if "allOf" in prop else prop
        if "default" in flat:
            defaults[name] = copy.deepcopy(flat["default"])

    if "outcomes" not in defaults and step_type in _OUTCOMES_OVERRIDE:
        defaults["outcomes"] = copy.deepcopy(_OUTCOMES_OVERRIDE[step_type])

    return defaults


# Hand-verified outcomes defaults for step types whose schema declares none.
# Sourced from a real, working element's actual data (get_element on a real
# workflow_approval step, probes/inspect_approval_outcomes.py, 2026-08-11) —
# linkID stripped, since that value belongs to that one already-wired
# instance and would make every freshly created approval step look like its
# Approve/Deny were already connected, which resolve_outcome would then
# refuse to wire to anything.
_OUTCOMES_OVERRIDE: dict[str, list[dict]] = {
    "workflow_approval": [
        {"id": 1, "outcomeID": 1, "type": "APPROVE", "text": "Approve",
         "buttonColor": "#01bd6f", "textColor": "#fff", "outcomeSign": "Yes"},
        {"id": 2, "outcomeID": 2, "type": "DENY", "text": "Deny",
         "buttonColor": "#D53049", "textColor": "#fff", "outcomeSign": "No"},
    ],
}


# Hand-verified shape for one entry of a workflow_conditional_branch's
# `outcomes` array, sourced from a real element with three named custom
# branches (get_element on a real workflow_conditional_branch step,
# probes/inspect_conditional_branch_outcomes.py, 2026-08-12) — never
# guessed. This only covers a *custom* branch (conditionValue "CUSTOM");
# the schema declares no default at all for outcomes on this type (unlike
# workflow_approval, which at least had a real default), so there is no
# starting point to build a new named branch from without this.
#
# `branchName` is the human label — NOT conditionValue, which is the
# fixed literal "CUSTOM" on every custom branch and cannot distinguish
# them (this was the actual bug behind connect_steps failing to resolve
# a branch by name; see tree_builder.outcome_label). `conditionTerms[].field`
# must be a real form field id from get_form_fields — this override
# describes the shape, it does not supply or invent field ids.
#
# Not injected via get_field_defaults (unlike _OUTCOMES_OVERRIDE above):
# a conditional branch's whole point is user-chosen names and conditions,
# so auto-filling a default branch would be actively wrong, not just
# unhelpful. This is exposed instead as an item_fields hint on
# get_step_schema, for the model to fill in and pass explicitly in
# add_step's config.
_OUTCOME_ITEM_FIELDS_OVERRIDE: dict[str, dict[str, str]] = {
    "workflow_conditional_branch": {
        "branchName": "string — the label shown on this branch, e.g. \"High priority\"",
        "conditionValue": (
            'string — always the literal "CUSTOM" for a named branch. '
            "This is NOT the branch name (that's branchName) — every "
            "custom branch on the same step shares this same value."
        ),
        "conditionTermsMatchType": 'string — "All" (AND) or "Any" (OR) across conditionTerms',
        "conditionTerms": (
            "array of {field, operator, value} — field must be a real "
            "form field id from get_form_fields, never invented. Confirmed "
            'operators seen in real data: "isEmpty", "isFilled". Others '
            "(e.g. equality/comparison operators) are plausible but "
            "unconfirmed — verify against a real element before relying "
            "on one not listed here."
        ),
    },
}


def list_types(category: str | None = None) -> list[dict[str, Any]]:
    """
    One line per step type. Internal types are excluded by default.

    Types we categorise but hold no schema for are still listed, flagged with
    schema_available=False — hiding them would make the model believe they
    don't exist, when a user's workflow may already contain one.
    """
    schemas = _load()
    if category:
        names = CATEGORIES.get(category, [])
    else:
        names = [n for cat, types in CATEGORIES.items() if cat != "internal" for n in types]

    return [
        {
            "step_type": name,
            "category": next((c for c, t in CATEGORIES.items() if name in t), "other"),
            "description": DESCRIPTIONS.get(name, ""),
            "ui_name": UI_NAMES.get(name),
            "schema_available": name in schemas,
        }
        for name in names
    ]