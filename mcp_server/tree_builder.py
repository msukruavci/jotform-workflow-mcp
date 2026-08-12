"""
Turns intent into `updateTree` payloads.

Why this file exists and the tools don't do this inline: everything in here
is a pure function over plain data, provable without a network call or an
API key. tools/building.py fetches state and calls the API; this decides
*what* to send. That split is what makes the hard part of Phase 3
unit-testable at all.

Every rule below was measured, not assumed — see docs/gap-report.md for the
probes that produced each one:

  - A link write needs `type`, `points`, `fromPortName`, `toPortName` all
    present. `points` is a presence check only — Jotform's server ignores
    its contents (probes/test_link_ports2.py). Port *names* are also
    unchecked; nonsense values are silently rewritten to the canonical pair.
    `type` is NOT corrected — a typo there produces a broken link with no
    error, so it is never taken from caller input. (Gap 2)

  - Branch identity is not on the link at all. It lives on the *deciding*
    element, as `outcomes[] = {outcomeID, conditionValue, linkID}`. Wiring a
    branch means writing the link with the constant payload below, then
    `action:"update"`-ing the source element's `outcomes` so the right
    `outcomeID` points at the new `linkID`. Confirmed end-to-end, including
    read-back, in probes/test_outcome_write.py. (Gaps 1 and 2)
"""
from __future__ import annotations

from mcp_server import schema_registry

# The one payload shape gap 2 confirmed works for any link, regardless of
# what the two ends actually are. Port names are cosmetic (server rewrites
# them); `type` is not, so it is a constant, never a parameter.
LINK_DEFAULTS = {
    "type": "default-link",
    "points": [{"a": "1"}],
    "fromPortName": "DYNAMIC_BOTTOM_1_Out",
    "toPortName": "DYNAMIC_TOP_1_In",
}

DEFAULT_ELEMENT_SIZE = {"width": 296, "height": 88}

# Layout spacing. Nothing in Jotform's API computes this for us (unlike
# ports) — canvas position is genuinely on us. Values are the element size
# above plus enough gap that two adjacent nodes don't touch.
STEP_Y = 180
BRANCH_X = 340


class ValidationError(Exception):
    """A request that must not reach the API — caller error, not server error."""


# --------------------------------------------------------------------------
# id allocation
# --------------------------------------------------------------------------

def next_id(existing: list[str | int | None]) -> int:
    """
    Element ids and link ids are both caller-assigned small integers in
    separate namespaces. Jotform doesn't hand out the next one — the client
    picks, so picking one already in use is a real way to silently
    overwrite something. Always compute from a fresh fetch, not a cached
    list from earlier in a longer conversation.
    """
    nums = []
    for v in existing:
        try:
            nums.append(int(v))
        except (TypeError, ValueError):
            continue
    return (max(nums) + 1) if nums else 1


# --------------------------------------------------------------------------
# layout
# --------------------------------------------------------------------------

def _position_of(el: dict) -> tuple[float, float] | None:
    pos = el.get("position") or {}
    x = pos.get("x", el.get("x"))
    y = pos.get("y", el.get("y"))
    if x is None or y is None:
        return None
    try:
        return float(x), float(y)
    except (TypeError, ValueError):
        return None


def compute_position(elements: list[dict], after_step_id: str | None) -> dict:
    """
    Where to put a new node.

    With no anchor: below the lowest existing node, so new work doesn't land
    on top of the workflow's start.

    With an anchor: directly below it, then nudged right by however many
    outgoing links the anchor already has — the first child goes straight
    down, the second one over, and so on. This does not attempt to be a
    real auto-layout (no collision detection against everything else on the
    canvas); it only guarantees a new node doesn't sit exactly on its
    parent. See docs/gap-report.md item 5 — a known open gap, not a solved
    one.
    """
    positioned = [p for p in (_position_of(e) for e in elements) if p is not None]

    if after_step_id is None:
        base_y = max((y for _, y in positioned), default=0)
        return {"x": 0, "y": base_y + STEP_Y}

    anchor = next(
        (e for e in elements if str(e.get("element_id")) == str(after_step_id)), None
    )
    anchor_pos = _position_of(anchor) if anchor else None
    if anchor_pos is None:
        base_y = max((y for _, y in positioned), default=0)
        return {"x": 0, "y": base_y + STEP_Y}

    ax, ay = anchor_pos
    return {"x": ax, "y": ay + STEP_Y}


# --------------------------------------------------------------------------
# element payloads
# --------------------------------------------------------------------------

def validate_config(step_type: str, config: dict) -> tuple[dict, list[str]]:
    """
    Filter caller-supplied fields against the simplified schema.

    Returns (clean_config, warnings). Unknown fields are dropped rather than
    sent through and rejected by the API — a model composing a config from
    a schema it read a few turns ago may include a field that doesn't exist;
    dropping it with a warning degrades gracefully, silently sending garbage
    would not. Enum fields are checked; a value outside `allowed_values` is
    also dropped rather than sent, since Jotform's own validation error for
    that case is not something we've observed and can't promise is safe.

    x/y/position are always stripped here — layout is computed by
    compute_position, never taken from the caller.
    """
    schema = schema_registry.get_simplified_schema(step_type)
    if schema is None:
        raise ValidationError(
            f"No schema for {step_type}; call get_step_schema first, or this "
            f"type has no schema on record and cannot be configured here."
        )

    by_name = {f["name"]: f for f in schema["fields"]}
    clean: dict = {}
    warnings: list[str] = []

    for key, value in (config or {}).items():
        if key in ("x", "y", "position", "type", "element_id", "id"):
            continue  # server-managed or set separately; never caller-supplied
        field = by_name.get(key)
        if field is None:
            warnings.append(f"unknown field '{key}' dropped")
            continue
        allowed = field.get("allowed_values")
        if allowed and value not in allowed:
            warnings.append(
                f"'{key}'={value!r} not in {allowed}; field dropped"
            )
            continue
        clean[key] = value

    return clean, warnings


def build_element_create(step_type: str, element_id: int, config: dict,
                         position: dict) -> dict:
    """
    The `elements[]` entry for creating one new step.

    Any field with a schema-declared default that the caller didn't
    supply gets filled in automatically — see
    schema_registry.get_field_defaults for why (short version: Jotform's
    server doesn't auto-default a handful of fields, and a missing one of
    those was the actual cause of a real blank-canvas render failure).
    This also covers branching types' TRUE/FALSE `outcomes`, which used
    to be a special case here and is now just one instance of the general
    rule.
    """
    data = {
        "element_id": element_id,
        "id": element_id,
        "type": step_type,
        "elementType": step_type,
        "position": position,
        "x": position["x"],
        "y": position["y"],
        "measured": DEFAULT_ELEMENT_SIZE,
        **config,
    }
    for field, default in schema_registry.get_field_defaults(step_type).items():
        if field not in data:
            data[field] = default
    return {"action": "create", "elementID": element_id, "data": data}


def build_element_update(element_id: int | str, config: dict) -> dict:
    """The `elements[]` entry for editing an existing step's fields."""
    data = {"element_id": element_id, **config}
    return {"action": "update", "elementID": element_id, "data": data}


# --------------------------------------------------------------------------
# link + outcome payloads
# --------------------------------------------------------------------------

def build_link_create(link_id: int, from_id: int | str, to_id: int | str) -> dict:
    data = {
        "link_id": link_id,
        "fromElement": from_id,
        "toElement": to_id,
        **LINK_DEFAULTS,
    }
    return {"action": "create", "linkID": link_id, "data": data}


# Different step types put an outcome's human-readable label under
# different keys. workflow_binary_decision uses `conditionValue` directly
# ("TRUE"/"FALSE") and so does the catch-all branch on
# workflow_conditional_branch ("OTHER"). But a *named* branch on
# conditional_branch is different: `conditionValue` is a fixed constant
# ("CUSTOM") on every custom branch — identical across all of them — the
# actual human name lives in `branchName`. Confirmed 2026-08-12 against a
# real conditional-branch element with three named branches
# (probes/inspect_conditional_branch_outcomes.py): checking conditionValue
# first, as this used to, made every custom branch resolve to the literal
# string "CUSTOM" — connect_steps could never find "branch 1" by name, and
# always wired to whichever custom branch happened to come first. `text`/
# `type` (workflow_approval's "Approve"/"Deny") stay as later fallbacks,
# checked in this order because `text` is what a person would naturally
# say; `type` is closer to an internal enum.
_OUTCOME_LABEL_FIELDS = ("branchName", "conditionValue", "text", "type")


def outcome_label(outcome: dict) -> str | None:
    for field in _OUTCOME_LABEL_FIELDS:
        value = outcome.get(field)
        if value:
            return str(value)
    return None


def resolve_outcome(source_element: dict, outcome: str) -> dict:
    """
    Find the outcome entry on a branching element matching the requested
    label (case-insensitive) — e.g. "true" matches conditionValue "TRUE",
    "approve" matches a workflow_approval outcome's text "Approve",
    "branch 1" matches a conditional-branch outcome's branchName. Matching
    strips whitespace as well as case — a real branchName was found with
    a trailing space ("branch 1 ") from how the user typed it in the
    builder, and a caller passing the clean name should still match it.

    Raises ValidationError, listing what's actually available, rather than
    silently connecting the wrong branch or creating an outcome that
    doesn't exist. Outcomes are configured on the element itself (an
    if/else always has TRUE/FALSE; a conditional branch's names are
    whatever the user set up) — this tool wires an existing outcome to a
    link, it does not invent new outcomes.
    """
    outcomes = source_element.get("outcomes") or []
    match = next(
        (o for o in outcomes
         if (outcome_label(o) or "").strip().lower() == outcome.strip().lower()),
        None,
    )
    if match is None:
        available = [outcome_label(o) for o in outcomes]
        raise ValidationError(
            f"'{outcome}' is not an outcome on this step. Available: {available}"
        )
    if match.get("linkID"):
        raise ValidationError(
            f"Outcome '{outcome}' is already connected (to element "
            f"{_target_of_link(match.get('linkID'))}). Use update_step to "
            f"change it, or pick a different outcome."
        )
    return match


def _target_of_link(link_id):  # pragma: no cover - message text only
    return f"link {link_id}"


def build_link_delete(link_id: int | str) -> dict:
    """
    The `links[]` entry for removing an existing link. Same shape
    risky.py's delete_step already uses to clean up incident links when
    a step is deleted (probes/test_delete_impact.py, 2026-08-10) —
    reused here for disconnect_steps, which removes a single link
    without deleting either step.
    """
    return {"action": "delete", "linkID": link_id, "data": {"link_id": link_id}}


def find_outcome_by_link(source_element: dict, link_id) -> dict | None:
    """
    Reverse of resolve_outcome: given a link_id, find which outcome on a
    branching element currently points at it. Used by disconnect_steps to
    know which outcome to clear — without this, deleting the link alone
    would leave the source element's outcome pointing at a link_id that no
    longer exists, which resolve_outcome would then wrongly read as "still
    connected" (blocking a future connect_steps call), and graph.py's
    dangling_links check would separately start flagging it.
    """
    for o in (source_element.get("outcomes") or []):
        if isinstance(o, dict) and str(o.get("linkID")) == str(link_id):
            return o
    return None


def build_outcome_update(source_element: dict, outcome_id, link_id: int | None) -> dict:
    """
    The `elements[]` entry that (re)wires an outcome's link, or clears it
    entirely when link_id is None — same write, either direction. Sends
    the *whole* outcomes array back, with only the matching entry's
    linkID changed — updateTree edits fields wholesale, so a partial
    outcomes list would drop the others.
    """
    outcomes = source_element.get("outcomes") or []
    updated = [
        {**o, "linkID": link_id} if o.get("outcomeID") == outcome_id else o
        for o in outcomes
    ]
    return build_element_update(source_element.get("element_id"), {"outcomes": updated})