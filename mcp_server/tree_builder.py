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

import re

from mcp_server import audit_log
from mcp_server import schema_registry
from mcp_server.integrations import SUPPORTED_WORKFLOW_INTEGRATION_SUBTYPES

# The one payload shape gap 2 confirmed works for any link, regardless of
# what the two ends actually are. Port names are cosmetic (server rewrites
# them); `type` is not, so it is a constant, never a parameter.
LINK_DEFAULTS = {
    "type": "default-link",
    "fromPortName": "DYNAMIC_BOTTOM_1_Out",
    "toPortName": "DYNAMIC_TOP_1_In",
}


def _normalize_after_unit(unit: str) -> str:
    normalized = str(unit).strip().lower()
    if normalized in ("day", "days", "gün", "gun", "günü", "gunu"):
        return "day"
    if normalized in ("hour", "hours", "saat"):
        return "hour"
    if normalized in ("week", "weeks", "hafta"):
        return "week"
    if normalized in ("month", "months", "ay"):
        return "month"
    return normalized

DEFAULT_ELEMENT_SIZE = {"width": 296, "height": 88}

# Layout spacing. Nothing in Jotform's API computes this for us (unlike
# ports) — canvas position is genuinely on us. Values are the element size
# above plus enough gap that two adjacent nodes don't touch.
STEP_Y = 220
BRANCH_X = 380


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


def _size_of(el: dict) -> tuple[float, float]:
    measured = el.get("measured") or {}
    try:
        return (
            float(measured.get("width", DEFAULT_ELEMENT_SIZE["width"])),
            float(measured.get("height", DEFAULT_ELEMENT_SIZE["height"])),
        )
    except (TypeError, ValueError):
        return float(DEFAULT_ELEMENT_SIZE["width"]), float(DEFAULT_ELEMENT_SIZE["height"])


def _overlaps(elements: list[dict], candidate_x: float, candidate_y: float, padding: float = 24.0) -> bool:
    new_width = DEFAULT_ELEMENT_SIZE["width"] + padding
    new_height = DEFAULT_ELEMENT_SIZE["height"] + padding
    cand_x = candidate_x - padding / 2.0
    cand_y = candidate_y - padding / 2.0
    for element in elements:
        position = _position_of(element)
        if position is None:
            continue
        x, y = position
        width, height = _size_of(element)
        elem_x = x - padding / 2.0
        elem_y = y - padding / 2.0
        elem_w = width + padding
        elem_h = height + padding
        if (
            cand_x < elem_x + elem_w
            and cand_x + new_width > elem_x
            and cand_y < elem_y + elem_h
            and cand_y + new_height > elem_y
        ):
            return True
    return False


def _vertical_edge_crosses(
    elements: list[dict],
    candidate_x: float,
    parent_y: float,
    candidate_y: float,
    *,
    ignore_ids: set[str] | None = None,
    padding: float = 12.0,
) -> bool:
    """Return true when a same-lane parent->child edge would run through an existing node."""
    top = min(parent_y, candidate_y)
    bottom = max(parent_y, candidate_y)
    if bottom - top <= DEFAULT_ELEMENT_SIZE["height"]:
        return False

    ignored = ignore_ids or set()
    for element in elements:
        if str(element.get("element_id")) in ignored:
            continue
        position = _position_of(element)
        if position is None:
            continue
        x, y = position
        width, height = _size_of(element)
        left = x - padding
        right = x + width + padding
        node_top = y + padding
        node_bottom = y + height - padding
        if left <= candidate_x <= right and node_top > top and node_bottom < bottom:
            return True
    return False


def compute_position(
    elements: list[dict],
    after_step_id: str | int | list[str | int] | None,
    *,
    branch_offset: float = 0,
) -> dict:
    """
    Where to put a new node.

    With no anchor: below the lowest existing node, so new work doesn't land
    on top of the workflow's start.

    With an anchor (or list of parent anchors): start below it/them.
    If after_step_id is a list of parent ids (e.g. for merge/join nodes),
    it centers horizontally at the average X of the parents and places below the lowest parent.
    `branch_offset` selects a branch column. If that rectangle is occupied, search
    neighbouring columns, then lower rows.
    """
    positioned = [p for p in (_position_of(e) for e in elements) if p is not None]

    if not after_step_id:
        base_y = max((y for _, y in positioned), default=0)
        cand_y = base_y + (STEP_Y if positioned else 0)
        while _overlaps(elements, 0, cand_y):
            cand_y += STEP_Y
        return {"x": 0, "y": cand_y}

    if isinstance(after_step_id, list):
        parent_ids = {str(pid) for pid in after_step_id if pid is not None}
        parent_elems = [
            e for e in elements if str(e.get("element_id")) in parent_ids and _position_of(e) is not None
        ]
        if parent_elems:
            avg_x = sum(_position_of(e)[0] for e in parent_elems) / len(parent_elems)
            max_y = max(_position_of(e)[1] for e in parent_elems)
            base_x = avg_x + branch_offset * BRANCH_X
            base_y = max_y + STEP_Y

            x_offsets = [0]
            for column in range(1, 100):
                x_offsets.extend([column * BRANCH_X, -column * BRANCH_X])
            for row in range(100):
                y = base_y + row * STEP_Y
                for offset in x_offsets:
                    x = base_x + offset
                    if not _overlaps(elements, x, y):
                        return {"x": round(x, 1), "y": round(y, 1)}
            return {"x": round(base_x, 1), "y": round(base_y, 1)}
        after_step_id = after_step_id[0] if after_step_id else None

    anchor = next(
        (e for e in elements if str(e.get("element_id")) == str(after_step_id)), None
    )
    anchor_pos = _position_of(anchor) if anchor else None
    if anchor_pos is None:
        base_y = max((y for _, y in positioned), default=0)
        cand_y = base_y + (STEP_Y if positioned else 0)
        while _overlaps(elements, 0, cand_y):
            cand_y += STEP_Y
        return {"x": 0, "y": cand_y}

    ax, ay = anchor_pos
    base_y = ay + STEP_Y
    base_x = ax + branch_offset * BRANCH_X

    x_offsets = [0]
    for column in range(1, 100):
        x_offsets.extend([column * BRANCH_X, -column * BRANCH_X])

    for row in range(100):
        y = base_y + row * STEP_Y
        for offset in x_offsets:
            x = base_x + offset
            if not _overlaps(elements, x, y):
                return {"x": round(x, 1), "y": round(y, 1)}

    return {"x": round(base_x, 1), "y": round(base_y, 1)}


def _canonical_layout_ref(ref: str | int | None) -> str:
    value = str(ref or "").strip()
    return "start" if value.lower() in ("start", "1") else value


_PRIMARY_BRANCH_WORDS = {
    "approve", "approved", "true", "provisioned", "yes", "success", "succeeded",
    "complete", "completed", "resolved", "pass", "passed", "ok", "onay", "onayla",
    "basarili",
}
_ALTERNATIVE_BRANCH_WORDS = {
    "reject", "rejected", "deny", "denied", "false", "unable", "no", "cancel",
    "cancelled", "canceled", "fail", "failed", "failure", "error", "exception",
    "other", "red", "reddet", "reddedildi",
}


def _semantic_branch_weight(label: str) -> int:
    """
    Sort branch lanes by intent: primary/success paths left, alternatives right.

    This is deliberately heuristic. Outcome labels are authored by users and
    templates, so exact enum matching is too brittle for layout.
    """
    text = str(label or "").strip().lower()
    if not text:
        return 0
    tokens = set(re.findall(r"\w+", text))
    if "unable" in text or "cannot" in text or "can't" in text:
        return 1
    if tokens & _PRIMARY_BRANCH_WORDS:
        return -1
    if tokens & _ALTERNATIVE_BRANCH_WORDS:
        return 1
    return 0


def compute_layered_dag_positions(
    elements: list[dict],
    step_refs: list[str],
    connections: list[tuple[str, str, str]],
    *,
    start_step_id: str | int = 1,
) -> dict[str, dict]:
    """
    Compute holistic positions for a batch of new workflow steps.

    The bulk builder knows the whole DAG before it writes, so it can do better
    than repeatedly asking `compute_position` for local "below parent" slots:

    - Y is assigned from the longest parent path, so every child is strictly
      below its parent and merge nodes are below their lowest incoming parent.
    - X is assigned from recursive subtree widths, so nested branches reserve
      enough horizontal lane space for their descendants.
    - Branch outcome labels nudge success/approval lanes to the left and
      rejection/error lanes to the right.
    """
    ordered_refs = [str(ref) for ref in step_refs]
    step_set = set(ordered_refs)
    if not ordered_refs:
        return {}

    start_elem = next(
        (e for e in elements if str(e.get("element_id")) == str(start_step_id)),
        None,
    ) or next((e for e in elements if e.get("type") == "workflow_start_point"), None)
    start_pos = _position_of(start_elem) if start_elem else None
    start_x, start_y = start_pos if start_pos is not None else (0.0, 0.0)

    existing_positions = [p for p in (_position_of(e) for e in elements) if p is not None]
    max_existing_y = max((y for _, y in existing_positions), default=start_y)

    parents: dict[str, list[str]] = {ref: [] for ref in ordered_refs}
    children: dict[str, list[str]] = {"start": []}
    edge_labels: dict[tuple[str, str], str] = {}
    connection_index: dict[tuple[str, str], int] = {}

    for idx, (raw_from, raw_to, outcome) in enumerate(connections or []):
        from_ref = _canonical_layout_ref(raw_from)
        to_ref = _canonical_layout_ref(raw_to)
        if to_ref not in step_set:
            continue
        if from_ref in step_set or from_ref == "start":
            if from_ref not in children:
                children[from_ref] = []
            if to_ref not in children[from_ref]:
                children[from_ref].append(to_ref)
            if from_ref not in parents[to_ref]:
                parents[to_ref].append(from_ref)
            edge_labels[(from_ref, to_ref)] = str(outcome or "")
            connection_index[(from_ref, to_ref)] = idx

    for ref in ordered_refs:
        children.setdefault(ref, [])

    def ordered_children(ref: str) -> list[str]:
        refs = children.get(ref, [])
        return sorted(
            refs,
            key=lambda child: (
                _semantic_branch_weight(edge_labels.get((ref, child), "")),
                connection_index.get((ref, child), 10_000),
            ),
        )

    indegree = {ref: 0 for ref in ordered_refs}
    for from_ref, to_refs in children.items():
        if from_ref not in step_set:
            continue
        for to_ref in to_refs:
            if to_ref in indegree:
                indegree[to_ref] += 1

    queue = [ref for ref in ordered_refs if indegree[ref] == 0]
    topo: list[str] = []
    while queue:
        ref = queue.pop(0)
        topo.append(ref)
        for child in children.get(ref, []):
            if child not in indegree:
                continue
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    topo.extend(ref for ref in ordered_refs if ref not in topo)

    y_by_ref: dict[str, float] = {"start": start_y}
    for ref in topo:
        parent_ys = [
            y_by_ref[parent]
            for parent in parents.get(ref, [])
            if parent in y_by_ref
        ]
        if parent_ys:
            y_by_ref[ref] = max(parent_ys) + STEP_Y
        else:
            y_by_ref[ref] = max_existing_y + STEP_Y

    width_cache: dict[tuple[str, bool], float] = {}

    def subtree_width(ref: str, *, through_parent: bool = False, visiting: set[str] | None = None) -> float:
        cache_key = (ref, through_parent)
        if cache_key in width_cache:
            return width_cache[cache_key]
        if through_parent and len(parents.get(ref, [])) > 1:
            width_cache[cache_key] = 1.0
            return 1.0
        visiting = set(visiting or set())
        if ref in visiting:
            return 1.0
        visiting.add(ref)
        child_widths = [
            subtree_width(child, through_parent=True, visiting=visiting)
            for child in ordered_children(ref)
            if child in step_set
        ]
        width = max(1.0, sum(child_widths) if child_widths else 1.0)
        width_cache[cache_key] = width
        return width

    lane_by_ref: dict[str, float] = {}

    def assign_children(ref: str, center_lane: float) -> None:
        child_refs = [child for child in ordered_children(ref) if child in step_set]
        if not child_refs:
            return
        total_width = sum(subtree_width(child, through_parent=True) for child in child_refs)
        cursor = center_lane - (total_width - 1.0) / 2.0
        for child in child_refs:
            child_width = subtree_width(child, through_parent=True)
            child_center = cursor + (child_width - 1.0) / 2.0
            if len(parents.get(child, [])) <= 1:
                assign_subtree(child, child_center)
            cursor += child_width

    def assign_subtree(ref: str, center_lane: float) -> None:
        if ref in lane_by_ref:
            return
        lane_by_ref[ref] = center_lane
        assign_children(ref, center_lane)

    root_refs = []
    for child in ordered_children("start"):
        if child in step_set and child not in root_refs:
            root_refs.append(child)
    for ref in ordered_refs:
        if not parents.get(ref) and ref not in root_refs:
            root_refs.append(ref)

    total_root_width = sum(subtree_width(ref) for ref in root_refs) if root_refs else 1.0
    cursor = 0.0 - (total_root_width - 1.0) / 2.0
    for ref in root_refs:
        width = subtree_width(ref)
        assign_subtree(ref, cursor + (width - 1.0) / 2.0)
        cursor += width

    for ref in topo:
        parent_lanes = [lane_by_ref[parent] for parent in parents.get(ref, []) if parent in lane_by_ref]
        if len(parent_lanes) > 1:
            weights = [_semantic_branch_weight(edge_labels.get((p, ref), "")) for p in parents.get(ref, [])]
            avg_weight = sum(weights) / len(weights) if weights else 0
            
            if avg_weight > 0.3:
                lane_by_ref[ref] = max(parent_lanes) + 1.0
            elif avg_weight < -0.3:
                lane_by_ref[ref] = min(parent_lanes) - 1.0
            else:
                lane_by_ref[ref] = sum(parent_lanes) / len(parent_lanes)
                
            assign_children(ref, lane_by_ref[ref])
        elif ref not in lane_by_ref:
            lane_by_ref[ref] = max(lane_by_ref.values(), default=0.0) + 1.0
            assign_children(ref, lane_by_ref[ref])

    raw_positions = {
        ref: {
            "x": start_x + lane_by_ref.get(ref, 0.0) * BRANCH_X,
            "y": y_by_ref.get(ref, max_existing_y + STEP_Y),
        }
        for ref in ordered_refs
    }

    placed = list(elements)
    final_positions: dict[str, dict] = {}

    def placed_parent_position(ref: str) -> tuple[dict | None, str | None]:
        if ref == "start":
            return ({"x": start_x, "y": start_y}, str(start_step_id))
        if ref in final_positions:
            return final_positions[ref], f"layout:{ref}"
        return None, None

    def edge_crosses_placed(ref: str, candidate_x: float, candidate_y: float) -> bool:
        for parent in parents.get(ref, []):
            parent_pos, parent_id = placed_parent_position(parent)
            if parent_pos is None or parent_id is None:
                continue
            parent_x = float(parent_pos["x"])
            parent_y = float(parent_pos["y"])
            if _vertical_edge_crosses(
                placed,
                candidate_x,
                parent_y,
                candidate_y,
                ignore_ids={parent_id},
            ):
                return True
        return False

    for ref in sorted(ordered_refs, key=lambda item: (raw_positions[item]["y"], raw_positions[item]["x"])):
        base_x = raw_positions[ref]["x"]
        y = raw_positions[ref]["y"]
        ref_parents = parents.get(ref, [])
        if len(ref_parents) == 1:
            parent = ref_parents[0]
            if (
                parent in final_positions
                and parent in raw_positions
                and abs(raw_positions[parent]["x"] - base_x) <= 1.0
            ):
                base_x = final_positions[parent]["x"]
        x_offsets = [0.0]
        for column in range(1, 100):
            x_offsets.extend([-column * BRANCH_X, column * BRANCH_X])
        x = base_x
        for offset in x_offsets:
            candidate_x = base_x + offset
            if not _overlaps(placed, candidate_x, y) and not edge_crosses_placed(ref, candidate_x, y):
                x = candidate_x
                break
        pos = {"x": round(x, 1), "y": round(y, 1)}
        final_positions[ref] = pos
        placed.append({
            "element_id": f"layout:{ref}",
            "position": pos,
            "measured": DEFAULT_ELEMENT_SIZE,
        })

    return final_positions


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
    resolved = schema_registry.resolve_step_type(step_type)
    canonical_type = resolved["canonical_type"]
    config = dict(config or {})

    if canonical_type == "workflow_pause":
        after_amount = config.pop("afterAmount", None)
        after_unit = config.pop("afterUnit", None)
        pause = config.get("pause")
        if not isinstance(pause, dict):
            pause = {"activated": "Yes", "executeWhen": {}}
            config["pause"] = pause
        execute_when = pause.get("executeWhen")
        if not isinstance(execute_when, dict):
            execute_when = {}
            pause["executeWhen"] = execute_when
        if "afterAmount" in pause and "afterAmount" not in execute_when:
            execute_when["afterAmount"] = str(pause.pop("afterAmount"))
        if "afterUnit" in pause and "afterUnit" not in execute_when:
            execute_when["afterUnit"] = _normalize_after_unit(pause.pop("afterUnit"))
        if after_amount is not None:
            execute_when["afterAmount"] = str(after_amount)
        if after_unit is not None:
            execute_when["afterUnit"] = _normalize_after_unit(after_unit)
        elif "afterUnit" in execute_when:
            execute_when["afterUnit"] = _normalize_after_unit(execute_when["afterUnit"])
        if "afterAmount" in execute_when:
            execute_when["afterAmount"] = str(execute_when["afterAmount"])

    schema = schema_registry.get_simplified_schema(step_type)
    if schema is None:
        raise ValidationError(
            f"No schema for {step_type}; call get_step_schema first, or this "
            f"type has no schema on record and cannot be configured here."
        )

    by_name = {f["name"]: f for f in schema["fields"]}
    clean: dict = {}
    warnings: list[str] = []

    if canonical_type == "workflow_integration":
        subtype = str(config.get("subType") or "").strip()
        if not subtype:
            raise ValidationError(
                "workflow_integration requires subType. Use one supported integration ID "
                "and leave authentication/settings blank."
            )
        if subtype not in SUPPORTED_WORKFLOW_INTEGRATION_SUBTYPES:
            raise ValidationError(
                f"Unsupported workflow integration subType {subtype!r}. "
                f"Allowed values: {SUPPORTED_WORKFLOW_INTEGRATION_SUBTYPES}"
            )
        clean = {
            "name": str(config.get("name") or subtype).strip(),
            "subType": subtype,
            "actionType": "",
            "integrationAccountID": "",
            "integrationID": "",
            "internalFormID": "",
            "mode": "",
            "responseMap": [],
        }
        allowed_shell_fields = set(clean)
        for key in config:
            if key not in allowed_shell_fields and key not in ("type", "element_id", "id", "x", "y", "position"):
                warnings.append(f"workflow_integration shell ignored config field '{key}'")
        return clean, warnings

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
        if canonical_type == "workflow_conditional_branch" and key == "outcomes":
            value = _validate_conditional_branch_outcomes(value)
        if canonical_type == "workflow_assign_task" and key == "outcomes":
            value = _validate_task_outcomes(value)
        if canonical_type == "workflow_approval" and key == "outcomes":
            value = _validate_approval_outcomes(value)
        clean[key] = value

    return clean, warnings


def _validate_approval_outcomes(outcomes) -> list[dict]:
    if not isinstance(outcomes, list) or not outcomes:
        return [
            {"id": 1, "outcomeID": 1, "type": "APPROVE", "name": "Approve", "text": "Approve",
             "buttonColor": "#01bd6f", "textColor": "#fff", "outcomeSign": "Yes"},
            {"id": 2, "outcomeID": 2, "type": "DENY", "name": "Deny", "text": "Deny",
             "buttonColor": "#D53049", "textColor": "#fff", "outcomeSign": "No"},
        ]

    normalized = []
    default_types = ["APPROVE", "DENY"]
    default_colors = ["#01bd6f", "#D53049"]
    default_signs = ["Yes", "No"]

    for idx, item in enumerate(outcomes, start=1):
        if isinstance(item, str):
            item_text = item.strip()
            o_type = default_types[idx - 1] if idx <= 2 else "CUSTOM"
            o_color = default_colors[idx - 1] if idx <= 2 else "#0075E3"
            o_sign = default_signs[idx - 1] if idx <= 2 else "No"
            normalized.append({
                "id": idx,
                "outcomeID": idx,
                "type": o_type,
                "name": "Approve" if idx == 1 else ("Deny" if idx == 2 else item_text),
                "text": item_text,
                "buttonColor": o_color,
                "textColor": "#fff",
                "outcomeSign": o_sign,
            })
        elif isinstance(item, dict):
            outcome_id = item.get("outcomeID") or item.get("id") or idx
            try:
                outcome_id = int(outcome_id)
            except (TypeError, ValueError):
                outcome_id = idx
            item_text = str(item.get("text") or item.get("name") or ("Approve" if idx == 1 else "Deny"))
            o_type = item.get("type") or (default_types[idx - 1] if idx <= 2 else "CUSTOM")
            o_color = item.get("buttonColor") or (default_colors[idx - 1] if idx <= 2 else "#0075E3")
            o_sign = item.get("outcomeSign") or (default_signs[idx - 1] if idx <= 2 else "No")
            item_dict = dict(item)
            item_dict.pop("linkID", None)
            normalized.append({
                **item_dict,
                "id": outcome_id,
                "outcomeID": outcome_id,
                "type": o_type,
                "name": item.get("name") or ("Approve" if idx == 1 else ("Deny" if idx == 2 else item_text)),
                "text": item_text,
                "buttonColor": o_color,
                "textColor": item.get("textColor") or "#fff",
                "outcomeSign": o_sign,
            })
    return normalized


def _task_outcome_object(outcome, idx: int) -> dict:
    if isinstance(outcome, dict):
        label = outcome.get("text") or outcome.get("branchName") or outcome.get("conditionValue")
        if not label:
            raise ValidationError(f"outcomes[{idx}] needs text.")
        outcome_id = outcome.get("outcomeID") or outcome.get("id") or idx
        try:
            outcome_id = int(outcome_id)
        except (TypeError, ValueError):
            raise ValidationError(f"outcomes[{idx}] id/outcomeID must be an integer.")
        outcome_dict = dict(outcome)
        outcome_dict.pop("linkID", None)
        return {
            **outcome_dict,
            "id": outcome_id,
            "outcomeID": outcome_id,
            "type": outcome.get("type") or "CUSTOM",
            "buttonColor": outcome.get("buttonColor") or "#0075E3",
            "text": str(label),
            "textColor": outcome.get("textColor") or "#FFFFFF",
        }
    if isinstance(outcome, str) and outcome.strip():
        return {
            "id": idx,
            "outcomeID": idx,
            "type": "CUSTOM",
            "buttonColor": "#0075E3",
            "text": outcome.strip(),
            "textColor": "#FFFFFF",
        }
    raise ValidationError(f"outcomes[{idx}] must be a non-empty string or object.")


def _validate_task_outcomes(outcomes) -> list[dict]:
    if not isinstance(outcomes, list) or not outcomes:
        raise ValidationError("workflow_assign_task.outcomes must be a non-empty list.")

    normalized = []
    seen_ids: set[int] = set()
    for idx, outcome in enumerate(outcomes, start=1):
        item = _task_outcome_object(outcome, idx)
        if item["outcomeID"] in seen_ids:
            raise ValidationError(f"Duplicate outcomeID {item['outcomeID']} in outcomes.")
        seen_ids.add(item["outcomeID"])
        normalized.append(item)
    return normalized


def _validate_conditional_branch_outcomes(outcomes) -> list[dict]:
    if not isinstance(outcomes, list) or not outcomes:
        raise ValidationError(
            "workflow_conditional_branch.outcomes must be a non-empty list. "
            "Use fields from create_form_with_ai or a fresh get_workflow, then "
            "provide at least one branch with conditionTerms using real form field ids."
        )

    normalized = []
    seen_ids: set[int] = set()
    for idx, outcome in enumerate(outcomes, start=1):
        if not isinstance(outcome, dict):
            raise ValidationError(f"outcomes[{idx}] must be an object.")

        condition_value = outcome.get("conditionValue")
        branch_name = outcome.get("branchName")
        is_other = condition_value == "OTHER"

        if not is_other and not branch_name:
            raise ValidationError(f"outcomes[{idx}] needs branchName.")

        terms = outcome.get("conditionTerms")
        if not is_other and not terms:
            raise ValidationError(
                f"Branch '{branch_name}' needs at least one conditionTerms "
                "entry. Do not create CUSTOM branches with conditionTerms=[]. "
                "Use a visible trigger form field label or a real field id, operator, and value."
            )
        if terms is None:
            terms = []
        if not isinstance(terms, list):
            raise ValidationError(f"Branch '{branch_name or condition_value}' conditionTerms must be a list.")

        normalized_terms = []
        for term_idx, term in enumerate(terms, start=1):
            if not isinstance(term, dict):
                raise ValidationError(
                    f"Branch '{branch_name or condition_value}' conditionTerms[{term_idx}] must be an object."
                )
            field = term.get("field")
            operator = term.get("operator")
            if not field:
                raise ValidationError(
                    f"Branch '{branch_name or condition_value}' conditionTerms[{term_idx}] needs field. "
                    "Use a visible trigger form field label or a field_id from trigger_form_fields."
                )
            if not operator:
                raise ValidationError(
                    f"Branch '{branch_name or condition_value}' conditionTerms[{term_idx}] needs operator."
                )
            normalized_term = {
                **term,
                "id": term.get("id") or f"term_{idx}_{term_idx}",
                "isError": bool(term.get("isError", False)),
            }
            if "value" not in normalized_term:
                normalized_term["value"] = ""
            normalized_terms.append(normalized_term)

        outcome_id = outcome.get("outcomeID") or outcome.get("id") or idx
        try:
            outcome_id = int(outcome_id)
        except (TypeError, ValueError):
            raise ValidationError(f"outcomes[{idx}] id/outcomeID must be an integer.")
        if outcome_id in seen_ids:
            raise ValidationError(f"Duplicate outcomeID {outcome_id} in outcomes.")
        seen_ids.add(outcome_id)

        outcome_dict = dict(outcome)
        outcome_dict.pop("linkID", None)
        normalized_outcome = {
            **outcome_dict,
            "id": outcome_id,
            "outcomeID": outcome_id,
            "type": outcome.get("type") or "CONDITION",
            "conditionValue": "OTHER" if is_other else "CUSTOM",
            "conditionTermsMatchType": outcome.get("conditionTermsMatchType") or "All",
            "conditionTerms": normalized_terms,
        }
        normalized.append(normalized_outcome)

    return normalized


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
    resolved = schema_registry.resolve_step_type(step_type)
    canonical_type = resolved["canonical_type"]
    data = {
        "element_id": element_id,
        "id": element_id,
        "type": canonical_type,
        "elementType": canonical_type,
        "position": position,
        "x": position["x"],
        "y": position["y"],
        "measured": DEFAULT_ELEMENT_SIZE,
        **config,
    }
    if resolved["subtype"]:
        data["subType"] = resolved["subtype"]
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
        "points": [{"a": "1"}],
        **LINK_DEFAULTS,
    }
    return {"action": "create", "linkID": link_id, "data": data}


def build_link_label_update(link_id: int | str, label: str) -> dict:
    """
    Add the builder-visible outcome label to an existing link.

    HAR capture from the Jotform builder showed that selecting an outcome on
    a link writes both sides: the source element's outcome gets linkID, and
    the link itself gets labels=[{justCreated, label}]. Without this label,
    the graph can be connected but the builder's "Select outcome" UI may not
    show the outcome as selected.
    """
    return {
        "action": "update",
        "linkID": link_id,
        "data": {
            "id": link_id,
            "link_id": link_id,
            "labels": [{"justCreated": True, "label": label}],
        },
    }


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
_OUTCOME_LABEL_FIELDS = ("branchName", "conditionValue", "text", "name", "type")


def outcome_label(outcome) -> str | None:
    if isinstance(outcome, str):
        return outcome
    if not isinstance(outcome, dict):
        return None
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
    match = None
    for idx, item in enumerate(outcomes, start=1):
        if (outcome_label(item) or "").strip().lower() == outcome.strip().lower():
            match = _task_outcome_object(item, idx) if isinstance(item, str) else item
            break
    if match is None:
        available = [outcome_label(o) for o in outcomes]
        raise ValidationError(
            f"'{outcome}' is not an outcome on this step. Available: {available}"
        )
    if match.get("linkID") not in (None, 0, "0", ""):
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


def build_outcome_clears_for_links(source_element: dict, link_ids: list) -> dict | None:
    """
    Clear every outcome on a branching source element that points at one of
    link_ids. Used when deleting a step removes multiple incident links:
    deleting the link objects alone is not enough, because the source
    element's outcome would still claim that branch is already wired.
    """
    wanted = {str(link_id) for link_id in link_ids}
    outcomes = source_element.get("outcomes") or []
    updated = []
    changed = False

    for outcome in outcomes:
        if isinstance(outcome, dict) and str(outcome.get("linkID")) in wanted:
            updated.append({**outcome, "linkID": None})
            changed = True
        else:
            updated.append(outcome)

    if not changed:
        return None
    return build_element_update(source_element.get("element_id"), {"outcomes": updated})


def build_outcome_update(source_element: dict, outcome_id, link_id: int | None) -> dict:
    """
    The `elements[]` entry that (re)wires an outcome's link, or clears it
    entirely when link_id is None — same write, either direction. Sends
    the *whole* outcomes array back, with only the matching entry's
    linkID changed — updateTree edits fields wholesale, so a partial
    outcomes list would drop the others.
    """
    try:
        wanted_id = int(outcome_id)
    except (TypeError, ValueError):
        wanted_id = outcome_id

    outcomes = source_element.get("outcomes") or []
    updated = []
    for idx, outcome in enumerate(outcomes, start=1):
        if isinstance(outcome, str):
            updated.append(
                {**_task_outcome_object(outcome, idx), "linkID": link_id}
                if idx == wanted_id else outcome
            )
            continue

        current_id = outcome.get("outcomeID") or outcome.get("id") or idx
        try:
            current_id = int(current_id)
        except (TypeError, ValueError):
            pass
        updated.append({**outcome, "linkID": link_id} if current_id == wanted_id else outcome)
    return build_element_update(source_element.get("element_id"), {"outcomes": updated})


for _traced_helper_name in (
    "compute_position",
    "compute_layered_dag_positions",
    "validate_config",
    "_validate_approval_outcomes",
    "_validate_task_outcomes",
    "_validate_conditional_branch_outcomes",
    "build_element_create",
    "build_element_update",
    "build_link_create",
    "build_link_label_update",
    "build_link_delete",
    "build_outcome_clears_for_links",
    "build_outcome_update",
):
    globals()[_traced_helper_name] = audit_log.trace_function(globals()[_traced_helper_name])
