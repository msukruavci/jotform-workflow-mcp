# Jotform Workflow MCP Server — Full Project README

`mcp_server/` — the actual product — is covered nearly line by line.
Everything else (`probes/`, `tests/`, `docs/`) is covered at the level of
"what it checks and what it proved," which is the level anyone but its
author needs.

If you only read one section before a mentor meeting, read
**"Findings that shaped the design"** near the end — it's the short list of
things that were wrong on the first try and why the current code doesn't
make the same mistake.

---

## Table of contents

1. [The project, in one paragraph](#the-project-in-one-paragraph)
2. [Repository layout](#repository-layout)
3. [Architecture](#architecture)
4. [`mcp_server/` — line by line](#mcp_server--line-by-line)
   - [`jotform_client.py`](#jotform_clientpy)
   - [`schema_registry.py`](#schema_registrypy)
   - [`graph.py`](#graphpy)
   - [`tree_builder.py`](#tree_builderpy)
   - [`models.py`](#modelspy)
   - [`tools/discovery.py`](#toolsdiscoverypy)
   - [`tools/reading.py`](#toolsreadingpy)
   - [`tools/building.py`](#toolsbuildingpy)
   - [`tools/risky.py`](#toolsriskypy)
   - [`server.py`](#serverpy)
5. [`tests/` — what's actually proven](#tests--whats-actually-proven)
6. [`probes/` — one line each](#probes--one-line-each)
7. [`docs/` — where the narrative lives](#docs--where-the-narrative-lives)
8. [Findings that shaped the design](#findings-that-shaped-the-design)
9. [Running the server](#running-the-server)
10. [Current status](#current-status)

---

## The project, in one paragraph

A six-week internship project: make Jotform Workflows reachable and
actionable from inside a conversation with an AI assistant (Claude,
ChatGPT), over MCP (Model Context Protocol), without leaving the
conversation. The scope, tool design, and interface were left open by the
brief on purpose — working that out was the assignment. This repo is one
MCP server exposing 14 tools across four layers (discovery, reading,
building, risky), built entirely against Jotform's **public**,
documented-and-undocumented API surface — never the internal BFF that
powers Jotform's own builder UI, which is session-gated and off-limits per
the project's ground rules.

## Repository layout

```
jotform-workflow-mcp/
├── mcp_server/       the product — everything the model actually talks to
│   ├── server.py           entry point, registers all tools
│   ├── jotform_client.py   HTTP wrapper around api.jotform.com
│   ├── schema_registry.py  turns Jotform's raw JSON Schemas into
│   │                       something a model can read
│   ├── graph.py             pure reachability/health analysis, no network
│   ├── tree_builder.py      pure functions: intent -> updateTree payload
│   ├── models.py            Pydantic return shapes for every tool
│   ├── schemas/
│   │   └── workflow_all_schemas.json   Jotform's raw element schemas
│   └── tools/
│       ├── discovery.py     list_step_types, get_step_schema
│       ├── reading.py       list_workflows, get_workflow, ...
│       ├── building.py      create_workflow, add_step, connect_steps, ...
│       └── risky.py         delete_step, publish_workflow, delete_workflow
├── tests/            unit tests — no network, no API key, run in <2s
│   ├── test_graph.py
│   └── test_tree_builder.py
├── probes/           one-off and repeatable scripts against the REAL
│   │                 Jotform API — this is where every claim in docs/
│   │                 traces back to
│   └── (~35 scripts — see the summary table below)
├── docs/
│   ├── gap-report.md      what's confirmed working, what isn't, and how
│   │                      each row was verified — the living source of
│   │                      truth for "what can this actually do"
│   └── decision-log.md    every non-obvious choice, dated, with the
│                          alternative considered and why it lost
├── bruno/            manual/interactive API testing collection
├── run_server.sh     entry point script — see "Running the server"
└── requirements.txt
```

## Architecture

```
 Person
   |
   v
 Claude / ChatGPT  <-- the model decides WHAT to do; it's the only
   |                   thing in this system that makes decisions
   | MCP protocol (stdio locally, or a remote connector)
   v
 mcp_server/  <-- decides HOW to do it, deterministically. Never asks
   |              its own LLM anything — there is no LLM in this box.
   v
 api.jotform.com
```

Four tool layers, built in this order, each depending only on the ones
before it:

1. **Discovery** — "what can I add?" No network beyond loading a local
   JSON file once.
2. **Reading** — "what does this workflow look like, and is it healthy?"
   Read-only, safe to call freely.
3. **Building** — "make this change." Writes, but nothing destructive.
4. **Risky** — delete and publish. Every tool here requires a second,
   explicit confirmation call before it does anything.

---

## `mcp_server/` — line by line

### `jotform_client.py`

The only file that talks HTTP. Everything above it works with Python
dicts; everything below it (the rest of this project) never imports
`requests` directly.

```python
BASE_URL = os.environ.get("JOTFORM_PUBLIC_API_BASE", "https://api.jotform.com")
TIMEOUT = 20
```
Base URL comes from the environment, not a hardcoded constant, because
Jotform has region-specific bases (EU/HIPAA) that might matter later. 20
second timeout — arbitrary but generous; nothing in this project has ever
needed longer.

```python
class JotformAPIError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"Jotform API error {status}: {body[:300]}")
        self.status = status
        self.body = body
```
A custom exception type, not just letting `requests` exceptions propagate.
Every tool in the four layers does `except JotformAPIError as e:` — a
narrow catch. If this were a bare `except Exception`, a real bug in *our*
code (a typo, a `None.get()`) would get silently swallowed and reported to
the model as "the API failed," which would be actively misleading during
debugging.

```python
class JotformClient:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("JOTFORM_API_KEY", "")
        if not self.api_key:
            raise ValueError("JOTFORM_API_KEY is not set")
```
Fails immediately and loudly if there's no key, at construction time —
not on the first API call three tool invocations later. `server.py`
constructs one `JotformClient` at import time, so a missing key kills the
whole server on boot with an unambiguous error, rather than each tool
call failing in a confusing way.

```python
def _request(self, method, path, *, params=None, json_body=None) -> dict:
    params = dict(params or {})
    params["apiKey"] = self.api_key
    resp = requests.request(method, f"{BASE_URL}{path}", params=params,
                             json=json_body, timeout=TIMEOUT)
    if resp.status_code not in (200, 201):
        raise JotformAPIError(resp.status_code, resp.text)
    return resp.json()
```
The single choke point every other method funnels through. `apiKey` gets
attached here once, so no method below has to remember to add it. The
leading underscore (`_request`) is a convention, not enforcement — it
signals "internal, callers outside this class shouldn't use this
directly," and every public method in the class does exactly that.

**Read methods** (all confirmed working, all read-only, safe to call
freely):

- `list_forms(status=None)` → `GET /user/forms`
- `get_form_questions(form_id)` → `GET /form/{id}/questions?parseJSON=1`
- `list_workflows()` → `GET /user/workflows` with a filter excluding
  DELETED/PURGED/ARCHIVED. **Not documented anywhere** — found via a HAR
  capture's `x-raw-uri` response header, which happened to reveal the
  server-side route name even though the client-facing URL was different.
- `get_workflow_combined(workflow_id)` → `GET /workflow/{id}/combined?fetchEssentialElementProps=1`
  — metadata + elements + links in **one** call. The docstring says
  "preferred over three separate calls" because it's independently
  confirmed to return every link the dedicated `/links` endpoint does (checked
  on two real workflows: 8/8 and 7/7 — see decision log, "Does /combined
  return every link").
- `get_workflow(workflow_id)` → `GET /workflow/{id}` — metadata only, no
  elements/links. Used where only title/status is needed (`delete_workflow`'s
  preview, for instance) to avoid pulling the whole tree unnecessarily.
- `get_elements(workflow_id)` → `GET /workflow/{id}/elements` — summarized
  node list.
- `get_element(workflow_id, element_id)` → `GET /workflow/{id}/elements/{id}`
  — the **full** config for one element, including `outcomes`,
  `conditionTerms`, everything the summary list omits. Every tool that
  needs to know exactly how a step is configured calls this, not
  `get_elements`.
- `get_links(workflow_id)` → `GET /workflow/{id}/links`

**Write methods:**

```python
def create_workflow(self, title: str, *, trigger_on_edit: str = "ENABLED") -> dict:
    return self._request("POST", "/workflow", json_body={
        "title": title, "triggerOnEdit": trigger_on_edit,
        "elements": [{"action": "update", "elementID": 1, "data": {
            "element_id": 1, "id": 1, "type": "workflow_start_point",
            "elementType": "workflow_start_point",
            "className": ["isStartPoint"],
            "position": {"x": 0, "y": 0}, "x": 0, "y": 0,
            "measured": {"width": 296, "height": 88},
        }}],
        "links": [],
    })
```
Every field of the start-point element here is copied verbatim from real
browser traffic — Jotform's UI sends exactly this shape when a new
workflow is created, including the odd `className: ["isStartPoint"]`
marker. Nobody has ever needed to know why these specific fields exist;
they're just what the server expects, and a model never sees them because
this whole method takes only a `title`.

```python
def create_element(self, workflow_id: str, step_type: str) -> dict:
    """Creates a bare element. Only `type` is required; config comes after."""
    return self._request("POST", f"/workflow/{workflow_id}/elements",
                          json_body={"type": step_type})
```
**This method is unused by every tool in this project.** It's a second,
independently-viable write path (`POST /elements`, create-then-configure)
that a separate exploration thread (`probes/build_branching_workflow.py`)
used successfully-in-appearance, derived from real browser traffic. Every
tool in this project instead uses `update_tree` with `action:"create"` in
one call — the path that was rigorously probe-verified with read-back
confirmation *in this project's own harness*. `create_element` is kept,
not deleted, as a documented alternative in case a future need (e.g.
wanting an element's assigned id before configuring it) makes the
two-call shape preferable. See decision log, 2026-08-10, "Standardized
element/link writes on updateTree."

```python
def update_tree(self, workflow_id: str, *, elements: list | None = None,
                links: list | None = None) -> dict:
    """
    The master endpoint — add/update/delete elements and links in one
    call. This is what Jotform's own UI uses for every change, and
    it's the most reliable write path we found.
    """
    return self._request("PUT", f"/workflow/{workflow_id}/updateTree",
                          json_body={"elements": elements or [], "links": links or []})
```
**The single most important method in this entire codebase.** Every
tool in `building.py` and `risky.py` ultimately calls this. Each entry in
`elements`/`links` has an `action` (`"create"` / `"update"` / `"delete"`),
an id, and a `data` dict. `tree_builder.py` exists entirely to build the
`data` dicts correctly.

```python
def set_trigger_form(self, workflow_id: str, form_id: str) -> dict:
    return self._request("POST", f"/workflow/{workflow_id}/setResource",
                          json_body={"resourceType": "FORM", "resourceID": form_id})
```
**Confirmed as a silent no-op on the public API** (2026-08-10,
`probes/inspect_trigger_binding.py`) — returns `true`, changes nothing,
anywhere. `building.py`'s `create_workflow` calls this but never trusts
the response; see that section below.

```python
def publish_workflow(self, workflow_id: str) -> dict:
    return self._request("POST", f"/workflow/{workflow_id}/publish")
```
Confirmed to be accepted (returns a structured object containing
`live: 1`), but the metadata fields checked to confirm its *effect*
(`publishStatus`, `hasPublishedFlow`) turned out to be unreliable — see
"Findings that shaped the design" below.

```python
def delete_workflow(self, workflow_id: str) -> dict:
    """
    Confirmed working 2026-08-10 (probes/test_delete_workflow.py) —
    DELETE /workflow/{id}, verified by checking the workflow no longer
    appears in list_workflows afterward, not just the 200 response.
    """
    return self._request("DELETE", f"/workflow/{workflow_id}")
```
Added this phase. `risky.py`'s `delete_workflow` tool wraps this with the
strictest confirmation pattern in the project (see that section).

---

### `schema_registry.py`

Loads Jotform's raw JSON Schema file (`schemas/workflow_all_schemas.json`,
36 step types, draft-07 JSON Schema) and turns it into something a model
can actually read, while keeping the raw version available for anything
needing real strictness.

```python
CATEGORIES = {
    "basic": [...8 types...],
    "logic": [...7 types...],
    "ai": [...13 types...],
    "integration": [...6 types...],
    "internal": [...3 types...],
}
```
36 types is too many to list flat — a model has to scan the whole thing
every time. Categories let `list_step_types(category="basic")` narrow
first. `"internal"` (placeholder/generic types Jotform auto-creates, never
meant to be added deliberately) is excluded from the default listing —
handled by `list_types()`'s `if cat != "internal"` filter.

```python
DESCRIPTIONS = { ... 36 entries ... }
```
Hand-written, one line per type. **Why not use Jotform's own schema
`title` field:** several AI step types (`workflow_ai_calculate`,
`workflow_ai_categorize`, `workflow_ai_summarize_text`,
`workflow_ai_sentiment_analysis`) are **all four titled "Webhook
Schema"** in Jotform's own schema file. A model choosing a step type by
reading that field would pick wrong every time. This dict is the fix —
tedious to maintain, but the alternative actively misleads.

```python
UI_NAMES = {
    "workflow_binary_decision": "If/Else Condition",
    "workflow_payment_verification": "Payment Form",  # UNCONFIRMED
    ...
}
```
What the Jotform **builder UI** calls each type, confirmed against a real
screenshot of the builder's left panel. Exists because the person talking
to the assistant just closed (or is looking at) that UI and says "add an
approval step" — the UI's vocabulary, not the API's type name. Two
entries are marked `UNCONFIRMED` — best guesses, not yet verified by
actually placing that element and reading the type back.

```python
UNMAPPED_UI_ELEMENTS = ["Approve & Sign", "Team Approval", "Flow Report", "PDF"]
```
Builder-UI elements seen in the screenshot with **no known type mapping
at all**. A concrete to-do, not a vague "schema might be incomplete."

```python
BRANCHING_TYPES = {"workflow_binary_decision", "workflow_conditional_branch"}
```
Step types where an outgoing link's meaning depends on a named outcome
(TRUE/FALSE, or a custom branch name) rather than just existing. **Shared**
between `reading.py` (labelling connections on the way out) and
`tree_builder.py`/`building.py` (deciding when `connect_steps` requires an
`outcome` argument). Defined once, here, specifically so the read side and
the write side can't drift into disagreeing about which types branch.

```python
def is_known_type(step_type: str) -> bool:
    return step_type in _load()
```
Live workflows can contain step types this project has no schema for
(`workflow_payment_verification` is a real, observed example). Every
caller that reports a step's type checks this and sets `known_type=False`
rather than crashing or pretending the type doesn't exist.

```python
def default_label(step_type: str | None) -> str:
    if not step_type:
        return "Unnamed step"
    return UI_NAMES.get(step_type) or step_type.replace("workflow_", "").replace("_", " ").capitalize()
```
Jotform leaves `name` empty on any step the user never manually renamed —
a workflow with three unlabelled email steps is unreadable to a model
("send this email" — which one?). Falls back to the UI name, then to a
prettified type name as a last resort.

```python
def _flatten_all_of(prop: dict) -> dict:
    if "allOf" not in prop:
        return prop
    merged = {k: v for k, v in prop.items() if k != "allOf"}
    for branch in prop["allOf"]:
        if isinstance(branch, dict):
            for k, v in branch.items():
                merged.setdefault(k, v)
    return merged
```
**The single highest-impact function in this file.** Jotform hides both
the real `$ref` (type info) and the `description` of every rich field —
`to`, `cc`, condition terms, anything that isn't a plain string/number —
inside a JSON Schema `allOf` wrapper. Before this function existed, an
email step's `to` field (the recipient list — arguably the single most
important field on that step) showed up to the model as
`{"name": "to", "type": "any"}` with no description at all. Measured
impact across all 36 schemas: **14 fields collapsed to `"any"`, 90 had no
description, before this fix; 2 and 78 after** (the remainder are
genuinely undocumented in Jotform's own schema — not fixable by
flattening).

```python
def _simplify_property(name: str, prop: dict, definitions: dict) -> dict:
    prop = _flatten_all_of(prop)
    if "$ref" in prop:
        ...
        items = resolved.get("items")
        if isinstance(items, dict) and isinstance(items.get("properties"), dict):
            entry["item_fields"] = {
                k: (v.get("type", "any") if isinstance(v, dict) else "any")
                for k, v in items["properties"].items()
                if k != "additionalProperties"
            }
        return entry
    ...
    if "const" in prop:
        entry["fixed_value"] = prop["const"]
    if "enum" in prop:
        entry["allowed_values"] = prop["enum"]
    if "anyOf" in prop:
        # enum/const is sometimes hidden inside an anyOf branch instead
        ...
```
For array-of-object fields (like `to`, which is an array of recipient
objects), `item_fields` shows what **one item** looks like — otherwise a
model knows it must send a list, but not a list of what. `allowed_values`
is the single most valuable piece of information this whole file
produces: it's the difference between a model guessing at a valid enum
value and knowing it exactly.

```python
def get_simplified_schema(step_type):
    ...
    fields = [f for f in fields if f["name"] not in ("x", "y")]
```
Canvas coordinates are stripped from what the model sees — deciding what
a step *does* shouldn't require thinking about where it sits visually.
(Positioning is computed separately, in `tree_builder.compute_position`.)

```python
def list_types(category=None):
    ...
    return [{
        "step_type": name, "category": ...,
        "description": DESCRIPTIONS.get(name, ""),
        "ui_name": UI_NAMES.get(name),
        "schema_available": name in schemas,
    } for name in names]
```
Types this file categorizes but holds no schema for (currently
`workflow_payment_verification`) are **still listed**, flagged
`schema_available: false`, rather than hidden. Hiding it would tell the
model a real, existing step type doesn't exist — worse than telling it
"this exists but I can't configure it for you."

---

### `graph.py`

The one file in this project with **zero** dependencies beyond the
standard library — no HTTP, no MCP, no Jotform-specific vocabulary beyond
the field names it's handed. That makes it the one part of the whole
system that's provable, not just plausible, which is why it has the most
unit tests per line of any file here.

```python
TERMINAL_TYPES = {"workflow_end_point"}
```
A step that's reached and has no outgoing link is normally a "dead end"
(a mistake) — except an explicit end-point, where that's correct and
intentional. This one-line exception exists so the health check doesn't
flag every properly-terminated branch as broken.

```python
def analyse(steps: list[dict], connections: list[dict]) -> dict:
```
Takes and returns plain dicts, not Pydantic models or anything
MCP-flavored — the boundary is deliberate, so this function can be
called with a hand-built fixture in a test with no server running.

```python
dangling = []
for c in connections:
    src, dst = c.get("from_step"), c.get("to_step")
    ...
    if src_s is not None and src_s not in id_set:
        dangling.append(f"link {c.get('link_id')}: from missing step {src_s}")
    elif dst_s is not None and dst_s not in id_set:
        dangling.append(f"link {c.get('link_id')}: to missing step {dst_s}")
```
Added ahead of confirming whether it was necessary — a defensive check
added *before* `probes/test_delete_impact.py` proved deleting a step
leaves its links behind pointing at nothing. Written as a safety net
regardless of the answer; turned out to be exactly the answer, and stayed
useful afterward as a general-purpose check for any future write path that
makes the same wrong assumption.

```python
roots = [i for i in ids if types.get(i) == "workflow_start_point"]
if not roots:
    roots = ids[:1]
```
Reachability is computed from the start point — if there isn't one (which
shouldn't happen, but nothing guarantees it), falls back to the first step
in the list rather than crashing, with the fallback being an explicit,
visible line rather than a silent assumption.

```python
seen: set[str] = set()
stack = list(roots)
while stack:
    node = stack.pop()
    if node in seen:
        continue
    seen.add(node)
    stack.extend(outgoing.get(node, []))
```
Plain iterative DFS. `if node in seen: continue` before adding to the
stack's next expansion means a cycle (step A links back to step B which
links back to A) can't cause an infinite loop — proven by
`test_cycle_does_not_hang` in the test suite, not just assumed.

```python
unreachable = [i for i in ids if i not in seen]
dead_ends = [
    i for i in ids
    if i in seen and not outgoing.get(i) and types.get(i) not in TERMINAL_TYPES
]
```
Two different problems, deliberately not conflated: **unreachable** =
never gets a chance to run at all. **Dead end** = does run, then the flow
just stops somewhere it wasn't supposed to. A workflow can have either
without the other, and a user needs to know which.

---

### `tree_builder.py`

**The most important file in the codebase.** Turns "add this kind of step
here" or "connect these two steps with this outcome" into the exact
`updateTree` payload Jotform's API will accept — and does it as pure
functions, no network calls, which is what makes it fully unit-testable
(24 tests, see below).

```python
LINK_DEFAULTS = {
    "type": "default-link",
    "points": [{"a": "1"}],
    "fromPortName": "DYNAMIC_BOTTOM_1_Out",
    "toPortName": "DYNAMIC_TOP_1_In",
}
```
The single most load-bearing constant in this project. Every one of these
four values was independently measured, not guessed:

- `points` — **must be non-empty; contents are completely ignored.** An
  empty list (`[]`) is rejected by the API as if the field were missing
  entirely (PHP `empty()` semantics — `[]` and absent are treated the
  same). `[{"a": "1"}]` is meaningless junk that happens to be non-empty,
  and is accepted and stored verbatim.
- `fromPortName` / `toPortName` — **required to be present, but their
  value is never validated.** Sending nonsense (`"BANANA_Out"`) is
  accepted, and the server **silently rewrites it to the real, correct
  port** on read-back. This was proven directly, not inferred: a link
  created with garbage port names, read back immediately after, showed
  the canonical port names Jotform's own UI would have used. This also
  proves ports **cannot** carry branch meaning — the server has no way to
  know which branch a link represents purely from the link, since it
  freely overwrites whatever port name it's given.
- `type` — required, **never validated, and never corrected.** A typo
  here (`"banana-link"` was tested) persists forever with **no error at
  write time and no visible symptom** until something downstream tries to
  interpret it. This asymmetry — ports self-heal, `type` doesn't — is
  exactly why `type` is a hardcoded constant here and appears nowhere in
  any tool's public parameters. No model, ever, can influence this value.

```python
STEP_Y = 180
BRANCH_X = 340
```
Layout spacing constants. `STEP_Y` (vertical gap between a step and
whatever gets placed after it) is used; `BRANCH_X` is declared but
**not currently used anywhere** — a leftover from an earlier plan for
side-by-side branch placement that was never implemented. Canvas layout
remains an open item (see gap-report.md item 5) — `compute_position`
below is a placeholder, not a solved auto-layout.

```python
class ValidationError(Exception):
    """A request that must not reach the API — caller error, not server error."""
```
Distinct from `JotformAPIError`. A `ValidationError` means "the tool layer
caught this before spending an API call" — e.g. an unknown step type, or
an outcome that doesn't exist. Every catch site in `building.py` and
`risky.py` distinguishes the two, because the right response to a model
differs: a `ValidationError` usually comes with a hint about what to try
instead; a `JotformAPIError` is closer to "something external went
wrong."

```python
def next_id(existing: list[str | int | None]) -> int:
    nums = []
    for v in existing:
        try:
            nums.append(int(v))
        except (TypeError, ValueError):
            continue
    return (max(nums) + 1) if nums else 1
```
Element ids and link ids are small integers the **caller** assigns —
Jotform doesn't hand one out. This always computes from a **freshly
fetched** list (every call site in `building.py` calls `client.get_elements`
or `client.get_links` immediately before calling this), never a cached
list from earlier in a longer conversation, because a stale id list means
picking an id already in use — silently overwriting something. The
`try/except` skips anything that isn't a clean integer (defensive against
Jotform occasionally mixing string/int ids across endpoints — observed
directly, see `test_ids_compare_as_strings_not_ints` in `test_graph.py`
for the parallel issue on the reading side).

```python
def compute_position(elements, after_step_id):
    positioned = [...]
    if after_step_id is None:
        base_y = max((y for _, y in positioned), default=0)
        return {"x": 0, "y": base_y + STEP_Y}
    anchor = next((e for e in elements if str(e.get("element_id")) == str(after_step_id)), None)
    anchor_pos = _position_of(anchor) if anchor else None
    if anchor_pos is None:
        base_y = max((y for _, y in positioned), default=0)
        return {"x": 0, "y": base_y + STEP_Y}
    ax, ay = anchor_pos
    return {"x": ax, "y": ay + STEP_Y}
```
Honest about what this is: **not** real auto-layout. No collision
detection against anything already on the canvas. With an anchor, the new
step goes directly below it. Without one, it goes below the lowest
existing step. Guarantees a new node never lands exactly on top of its
parent; does **not** guarantee it won't visually overlap something else
already on a busy canvas. Explicitly flagged as an open gap, not
presented as solved.

```python
def validate_config(step_type: str, config: dict) -> tuple[dict, list[str]]:
    schema = schema_registry.get_simplified_schema(step_type)
    if schema is None:
        raise ValidationError(...)
    by_name = {f["name"]: f for f in schema["fields"]}
    clean, warnings = {}, []
    for key, value in (config or {}).items():
        if key in ("x", "y", "position", "type", "element_id", "id"):
            continue
        field = by_name.get(key)
        if field is None:
            warnings.append(f"unknown field '{key}' dropped")
            continue
        allowed = field.get("allowed_values")
        if allowed and value not in allowed:
            warnings.append(f"'{key}'={value!r} not in {allowed}; field dropped")
            continue
        clean[key] = value
    return clean, warnings
```
Every field a model tries to set gets checked against the real schema.
Unknown fields are **dropped with a warning**, not rejected outright and
not silently sent through — a model working from a schema it read a few
conversation turns ago might include a field that doesn't exist or has
since changed; failing the whole request over one bad field would be
needlessly brittle, but silently sending garbage to the API would be
worse. Enum violations are handled the same way. Positioning and identity
fields (`x`, `y`, `position`, `type`, `element_id`, `id`) are silently
stripped here, every time — those are never the model's to set (see
`test_validate_config_strips_layout_and_identity_fields`).

```python
def _default_outcomes(step_type: str) -> list[dict] | None:
    raw = schema_registry.get_raw_schema(step_type) or {}
    default = ((raw.get("properties") or {}).get("outcomes") or {}).get("default")
    return copy.deepcopy(default) if isinstance(default, list) else None
```
**Fixes a real, measured bug.** Jotform's JSON Schema lists a `default`
value for `outcomes` on `workflow_binary_decision` (the standard TRUE/FALSE
pair) — but that `default` describes what a UI **client** should
pre-populate a form with; the **server does not apply it**. An if/else
element created via `updateTree` with no explicit `outcomes` array comes
back with **none**, permanently — `connect_steps` then has nothing to
attach a link to, ever, for that step. Discovered when every single
`connect_steps` call in an early integration test failed with
`Available outcomes: []` against a freshly-created decision step.
`copy.deepcopy` matters here — without it, every element created from the
same step type would share and mutate the same list object.

```python
def build_element_create(step_type, element_id, config, position):
    data = {
        "element_id": element_id, "id": element_id,
        "type": step_type, "elementType": step_type,
        "position": position, "x": position["x"], "y": position["y"],
        "measured": DEFAULT_ELEMENT_SIZE,
        **config,
    }
    if step_type in schema_registry.BRANCHING_TYPES and "outcomes" not in data:
        defaults = _default_outcomes(step_type)
        if defaults:
            data["outcomes"] = defaults
    return {"action": "create", "elementID": element_id, "data": data}
```
Both `element_id` and `id` are set — Jotform's raw payloads use both keys
in different places and this project never fully determined why; sending
both matches what's observed to work and costs nothing. The default-outcome
injection only fires if the caller (i.e., `validate_config`'s cleaned
output) didn't already supply `outcomes` — so a future caller that does
supply custom outcomes is respected, not overwritten (see
`test_caller_supplied_outcomes_are_not_overwritten`).

```python
def build_link_create(link_id, from_id, to_id):
    data = {"link_id": link_id, "fromElement": from_id, "toElement": to_id, **LINK_DEFAULTS}
    return {"action": "create", "linkID": link_id, "data": data}
```
Every link in this project, regardless of what the two ends are, gets
exactly this shape. There is no per-step-type variation in link payloads
— one of the more pleasant discoveries in this project, since the
original fear was needing a different port model per step type.

```python
def resolve_outcome(source_element: dict, outcome: str) -> dict:
    outcomes = source_element.get("outcomes") or []
    match = next((o for o in outcomes
                  if str(o.get("conditionValue", "")).lower() == outcome.lower()), None)
    if match is None:
        available = [o.get("conditionValue") for o in outcomes]
        raise ValidationError(f"'{outcome}' is not an outcome on this step. Available: {available}")
    if match.get("linkID"):
        raise ValidationError(
            f"Outcome '{outcome}' is already connected (to element "
            f"{_target_of_link(match.get('linkID'))}). ..."
        )
    return match
```
Case-insensitive match (`"true"` matches `"TRUE"`) — a model is as likely
to write either. Two distinct, named failure modes, both caught **before**
any API call: the outcome doesn't exist on this step at all, or it exists
but is already wired to something. Neither silently proceeds and connects
the wrong thing — this function only ever returns a *valid, unconnected*
outcome or raises.

```python
def build_outcome_update(source_element, outcome_id, link_id):
    outcomes = source_element.get("outcomes") or []
    updated = [
        {**o, "linkID": link_id} if o.get("outcomeID") == outcome_id else o
        for o in outcomes
    ]
    return build_element_update(source_element.get("element_id"), {"outcomes": updated})
```
Sends the **entire** `outcomes` array back, with only the matching entry
changed. `updateTree` replaces fields wholesale, not by merging — sending
just the one changed outcome would silently wipe out every other outcome
on that step (e.g. it would delete the FALSE branch while wiring the TRUE
one). This is the single most important line to understand about how
branch-wiring actually works end to end.

---

### `models.py`

Every tool's return type is a Pydantic model, not a bare `dict` or
`list[dict]`. This wasn't the original design — it was a fix. **Found via
MCP Inspector:** a tool annotated `-> dict` gives the MCP SDK nothing to
build a JSON Schema from, so the client receives that tool's richest data
(`get_workflow`'s entire step/connection/health structure, for instance)
as an unstructured text blob instead of something it can parse
structurally. Every single field across every model below exists because
some tool needs to return exactly that piece of information — there are
no speculative or "might need later" fields.

Organized into four groups matching the four tool layers:

- **Discovery**: `StepTypeSummary`, `StepTypeList`, `SchemaField`, `StepSchema`
- **Reading**: `WorkflowSummary`/`WorkflowList`, `Step`, `Connection`
  (carries `outcome` and `from_port` — see `reading.py` below for why
  both exist and mean very different things), `WorkflowHealth`
  (`unreachable_steps`, `dead_end_steps`, `dangling_links`,
  `unconnected_branches`, `unknown_types` — one field per distinct failure
  mode `graph.py` can detect), `WorkflowDetail`, `StepDetail`,
  `FormSummary`/`FormList`, `FormField`/`FormFieldList`
- **Building**: `CreateWorkflowResult`, `AddStepResult` (carries
  `warnings: list[str]` — every dropped/corrected config field surfaces
  here, never silently), `ConnectStepsResult`, `UpdateStepResult`
- **Risky**: `DeleteStepResult`, `PublishWorkflowResult`,
  `DeleteWorkflowResult` — every one of these three carries
  `needs_confirmation: bool` as a first-class field, not something
  inferred from other fields being empty.

Almost every model also carries an optional `error: str | None = None`.
This is deliberate: **tools in this project never raise exceptions up to
the model.** An exception tells a model nothing actionable; an `error`
field it can read, understand, and explain to the person. Every `try/except
JotformAPIError` block across `discovery.py`, `reading.py`, `building.py`,
`risky.py` ends by populating this field, not by letting the exception
propagate.

---

### `tools/discovery.py`

The smallest, simplest layer — two tools, both thin wrappers over
`schema_registry.py` with essentially no logic of their own.

```python
@mcp.tool()
def list_step_types(category: str = "") -> StepTypeList:
    """..."""
    return StepTypeList(
        step_types=[StepTypeSummary(**t) for t in schema_registry.list_types(category or None)]
    )
```
`category: str = ""` rather than `category: str | None = None` — the MCP
SDK's schema generation handles a plain string default more predictably
than an Optional across different client implementations; `category or
None` immediately below converts the empty-string default back to
`None` for `schema_registry.list_types`'s own signature. This same
pattern (`str = ""` at the tool boundary, converted internally) repeats
throughout `building.py` and `risky.py` for every optional string
parameter.

```python
@mcp.tool()
def get_step_schema(step_type: str) -> StepSchema:
    result = schema_registry.get_simplified_schema(step_type)
    if result is None:
        available = [t["step_type"] for t in schema_registry.list_types()]
        known = step_type in available
        if known:
            return StepSchema(
                step_type=step_type, ui_name=schema_registry.get_ui_name(step_type),
                error=f"No field schema on record for {step_type}.",
                hint="This is a real step type and may appear in existing workflows, "
                     "but this server cannot describe or configure its fields. "
                     "Tell the user it must be edited in Jotform.",
            )
        return StepSchema(
            error=f"Unknown step type: {step_type}",
            hint="Call list_step_types to see valid values.",
            available_types=available,
        )
    return StepSchema(...)
```
Two distinct error cases, deliberately not collapsed into one: a step
type that doesn't exist at all (typo, hallucination) versus a step type
that's real — might be sitting in the user's actual workflow right now —
but has no schema in this project's file. Telling a model "unknown type"
when the type is real and simply undocumented would cause it to tell the
user something false ("that step type doesn't exist"). The `hint` field
in each case tells the model exactly what to do next.

---

### `tools/reading.py`

Five tools. The most complex file in the reading layer by a wide margin —
`get_workflow` alone is nearly half the file — because it's where branch
identity, health analysis, and diagnostics all come together.

```python
def _outcome_map(elements: list[dict]) -> tuple[dict[str, str], list[str]]:
    mapping: dict[str, str] = {}
    unconnected: list[str] = []
    for el in elements:
        if el.get("type") not in BRANCHING_TYPES:
            continue
        step_id = el.get("element_id")
        for outcome in el.get("outcomes") or []:
            if not isinstance(outcome, dict):
                continue
            label = outcome.get("conditionValue") or outcome.get("value")
            link_id = outcome.get("linkID")
            if link_id in (None, 0, "0", ""):
                unconnected.append(f"step {step_id} {label or outcome.get('outcomeID')}")
            elif label:
                mapping[str(link_id)] = str(label)
    return mapping, unconnected
```
**This function is the fix for the biggest wrong turn in the whole
project.** The module docstring records it plainly:

> This module originally dropped everything about a link except its two
> endpoints, which lost the distinction between an if/else step's TRUE and
> FALSE branches — that is meaning, not plumbing.
>
> The obvious place to look for that label was the link. It isn't there:
> `labels` is empty on every link, and `fromPortName`
> ("RIGHT_MIDDLE_Out") describes where an edge leaves the box on the
> canvas, which happens to correlate with the branch and would have been
> a plausible, wrong answer. The label lives on the *deciding element*,
> as `outcomes[] = {conditionValue, linkID}`.

Concretely: on the one real if/else workflow available while investigating
this, `fromPortName` really did correlate with TRUE vs FALSE. Shipping
that as the answer would have passed every test run against that one
workflow and been wrong in general — the correlation was an accident of
that node's specific canvas layout, not a property of the field. The fix
came from reading Jotform's own raw JSON Schema for `workflow_binary_decision`,
which declares `outcomes` explicitly, rather than pattern-matching harder
on the one example at hand. `link_id in (None, 0, "0", "")` is
deliberately loose — Jotform has been observed sending an unset `linkID`
as any of `None`, `0`, `"0"`, or `""` depending on context, and all four
mean the same thing: this branch is defined but not wired to anything.

```python
@mcp.tool()
def get_workflow(workflow_id: str) -> WorkflowDetail:
    ...
    combined = client.get_workflow_combined(workflow_id)
    wf = combined.get("workflow", {}) or {}
    elements = [el for el in (combined.get("elements") or []) if isinstance(el, dict)]
    links = [ln for ln in (combined.get("links") or []) if isinstance(ln, dict)]
    outcome_by_link, unconnected_branches = _outcome_map(elements)
```
One API call (`/combined`) produces everything this tool needs — the
`isinstance(..., dict)` filters exist because Jotform's raw arrays have,
in practice, occasionally contained non-dict junk entries; filtering
defensively here means one malformed entry can't crash the whole tool.

```python
steps: list[Step] = []
unknown_types: list[str] = []
for el in elements:
    step_type = el.get("type")
    known = bool(step_type) and schema_registry.is_known_type(step_type)
    if step_type and not known and step_type not in unknown_types:
        unknown_types.append(step_type)
    steps.append(Step(
        step_id=str(el.get("element_id")) if el.get("element_id") is not None else None,
        type=step_type,
        label=el.get("name") or schema_registry.default_label(step_type),
        trigger_form_id=el.get("resourceID"),
        known_type=known,
    ))
```
Every step id is explicitly cast to `str` — Jotform mixes int and string
ids across different endpoints and even within the same response
depending on context (directly observed and specifically tested for in
`tests/test_graph.py`'s `test_ids_compare_as_strings_not_ints`), and
downstream comparisons throughout this project assume string ids
uniformly. `label` falls back through `el.get("name")` first (the user's
own label, if they set one) before `default_label`.

```python
connections = []
for ln in links:
    link_id = str(ln.get("link_id")) if ln.get("link_id") is not None else None
    connections.append(Connection(
        link_id=link_id,
        from_step=..., to_step=...,
        outcome=outcome_by_link.get(link_id or ""),
        from_port=ln.get("fromPortName"),
    ))
```
Both `outcome` and `from_port` are kept, deliberately, as two separate
fields meaning two very different things. `outcome` is what a model should
read and reason about — the actual branch identity. `from_port` is kept
purely because writing a link back later (in `tree_builder.py`) requires
*some* port value, and it's never shown to the model as meaningful — see
the correction note above for why treating it as meaningful was the
original mistake.

```python
health_raw = graph.analyse(
    [s.model_dump() for s in steps],
    [c.model_dump() for c in connections],
)
```
The Pydantic models are converted back to plain dicts specifically to
call into `graph.py` — reinforcing that `graph.py` genuinely has no
Pydantic/MCP dependency at all, even from its one caller.

```python
diagnostics: dict = {}
branching_ids = {str(el.get("element_id")) for el in elements
                 if el.get("type") in BRANCHING_TYPES}
unlabelled = sorted(
    sid for sid in branching_ids
    if any(c.from_step == sid for c in connections)
    and not any(c.from_step == sid and c.outcome for c in connections)
)
if unlabelled:
    diagnostics["unlabelled_branching_steps"] = unlabelled
    diagnostics["note"] = (...)
```
A self-check: if a step is known to branch, has at least one outgoing
connection, but **none** of its connections got an `outcome` label, that
means the outcome-mapping logic found nothing usable — possibly because
Jotform's data shape changed since this was written. Rather than silently
returning connections with `outcome: null` and letting that look like "no
branches here," this surfaces the discrepancy explicitly and points at
the specific probe script (`inspect_outcomes.py`) to re-run.

```python
@mcp.tool()
def get_step_details(workflow_id: str, step_id: str) -> StepDetail:
    ...
    config = client.get_element(workflow_id, step_id)
```
Deliberately calls `get_element` (the full, single-element endpoint), not
something derived from the already-fetched `get_workflow` data — because
`get_workflow`'s `steps` list is a summary by design (canvas noise
stripped), and this tool exists specifically for when that summary isn't
enough.

`list_forms` and `get_form_fields` are straightforward wrappers with no
comparable complexity — list forms with id/title/status/submission count;
list one form's fields with id/label/type/required, used for picking a
condition field or an email recipient field.

---

### `tools/building.py`

Four tools. Every one follows the same shape: fetch current state (never
trust anything cached from earlier in a conversation), hand it to
`tree_builder` to compute the payload, write it, report what happened.

```python
@mcp.tool()
def create_workflow(title: str, trigger_form_id: str = "") -> CreateWorkflowResult:
    ...
    created = client.create_workflow(title)
    workflow_id = created.get("id") or created.get("workflowID")
    ...
    if trigger_form_id:
        try:
            client.set_trigger_form(workflow_id, trigger_form_id)
            elements = client.get_elements(workflow_id)
            start = next((e for e in elements if e.get("type") == "workflow_start_point"), {})
            if str(start.get("resourceID")) != str(trigger_form_id):
                return CreateWorkflowResult(
                    workflow_id=str(workflow_id), title=title,
                    error=(
                        "Workflow created, but the trigger form could not be "
                        "bound — this is a known limitation of the public API, "
                        "not a failure you can retry. ..."
                    ),
                )
        except JotformAPIError as e:
            return CreateWorkflowResult(..., error=f"... trigger form failed: {e}")
    return CreateWorkflowResult(workflow_id=str(workflow_id), title=title,
                                trigger_form_id=trigger_form_id or None)
```
This is the newest logic in the whole project (2026-08-10) and exists
because of a real bug: `set_trigger_form` returns `true` and changes
nothing (confirmed by diffing every field on the workflow and its start
point, before and after, in `probes/inspect_trigger_binding.py` — zero
fields changed anywhere). The **old** version of this tool trusted that
`true` and reported success. This version reads the start point element
back and checks whether `resourceID` actually equals what was sent —
this is a **write verified by a read**, the same discipline every other
write tool in this project already followed. `create_workflow` was the
one place that discipline had lapsed, and it's the reason this bug went
unnoticed until it was specifically probed for.

```python
@mcp.tool()
def add_step(workflow_id, step_type, config, after_step_id=""):
    try:
        clean_config, warnings = tb.validate_config(step_type, config)
    except tb.ValidationError as e:
        return AddStepResult(error=str(e), hint="Call list_step_types to see valid values.")

    elements = client.get_elements(workflow_id)

    after_id = after_step_id or None
    if after_id is not None:
        links = client.get_links(workflow_id)
        existing_exit = next((l for l in links if str(l.get("fromElement")) == str(after_id)), None)
        if existing_exit is not None:
            return AddStepResult(
                error=f"Step {after_id} already has an outgoing connection (to step {existing_exit.get('toElement')}).",
                hint="Add this step without after_step_id, then use connect_steps ...",
            )

    element_id = tb.next_id([e.get("element_id") for e in elements])
    position = tb.compute_position(elements, after_id)
    create_entry = tb.build_element_create(step_type, element_id, clean_config, position)
    client.update_tree(workflow_id, elements=[create_entry])

    linked_from = None
    if after_id is not None:
        links = client.get_links(workflow_id)
        link_id = tb.next_id([l.get("link_id") for l in links])
        client.update_tree(workflow_id, links=[tb.build_link_create(link_id, after_id, element_id)])
        linked_from = str(after_id)

    return AddStepResult(step_id=str(element_id), type=step_type, linked_from=linked_from, warnings=warnings)
```
The `after_step_id` guard — refusing to auto-link if the anchor already
has an outgoing connection — is a deliberate safety choice, not an
API limitation. Nothing stops the code from just adding a second link;
the reason it doesn't is that a step with more than one exit needs
**deliberate** wiring (is this a new branch of an if/else? A parallel
path off a split? Those need different handling), and guessing would be
worse than asking the model to be explicit via `connect_steps`. Note
`update_tree` is called **twice** here when linking — once for the
element, once for the link — rather than in one combined call; this was
a design choice for clearer error attribution (if linking fails, the
step still exists and the error says so precisely, rather than the whole
operation failing atomically and leaving the model unsure what state the
workflow is actually in).

```python
@mcp.tool()
def connect_steps(workflow_id, from_step_id, to_step_id, outcome=""):
    source = client.get_element(workflow_id, from_step_id)
    source_type = source.get("type")
    is_branching = source_type in schema_registry.BRANCHING_TYPES

    if is_branching and not outcome:
        available = [o.get("conditionValue") for o in (source.get("outcomes") or [])]
        return ConnectStepsResult(error=f"{from_step_id} is a {source_type} and requires an outcome.",
                                  hint=f"Available outcomes: {available}")
    if not is_branching and outcome:
        return ConnectStepsResult(error=f"{from_step_id} ({source_type}) does not branch — it takes no outcome.")

    matched_outcome = None
    if is_branching:
        try:
            matched_outcome = tb.resolve_outcome(source, outcome)
        except tb.ValidationError as e:
            return ConnectStepsResult(error=str(e))

    links = client.get_links(workflow_id)
    link_id = tb.next_id([l.get("link_id") for l in links])
    client.update_tree(workflow_id, links=[tb.build_link_create(link_id, from_step_id, to_step_id)])

    if is_branching:
        try:
            client.update_tree(workflow_id, elements=[
                tb.build_outcome_update(source, matched_outcome["outcomeID"], link_id)
            ])
        except JotformAPIError as e:
            return ConnectStepsResult(
                link_id=str(link_id), from_step=from_step_id, to_step=to_step_id,
                error=f"Link created, but labelling the outcome failed: {e}. "
                      f"The steps are connected but the branch is unlabelled.",
            )

    return ConnectStepsResult(link_id=str(link_id), from_step=from_step_id,
                              to_step=to_step_id, outcome=outcome or None)
```
Notice the **order**: the link is written first, the outcome label second
— and if the second call fails, the error explicitly says the connection
exists but is unlabelled, rather than leaving the model to assume nothing
happened. This is the tool where branch identity actually gets created,
end to end — everything in `tree_builder.resolve_outcome` and
`build_outcome_update` exists to serve this one call site.

`update_step` is the simplest of the four: fetch the current element to
learn its type (needed because `validate_config` needs to know which
schema to check against), validate the new config, and if anything
survived validation, send an `action:"update"` for just those fields.
Explicitly does **not** touch position or connections — that's
`connect_steps`'s job, kept separate on purpose.

---

### `tools/risky.py`

Three tools — the only ones in the project that can destroy or publicize
data. Every one implements the same two-call pattern.

```python
"""
Every tool here follows a two-call pattern: call once and nothing happens —
you get back what *would* happen. Call again with confirm=True and it does.
...
Why this shape and not a yes/no prompt inside the tool: MCP tools are
synchronous request/response, there's no channel to pause mid-call and wait
for a person to answer.
"""
```
The module docstring states the reasoning directly: MCP has no built-in
"pause and ask the user" mechanism for a tool call. Forcing two separate
tool invocations is the only way to guarantee the model had to have
**already shown the preview and gotten a real answer** in the
conversation before the second call could be made — a model that sets
`confirm=True` on the first call is fabricating consent, not working
within a technical constraint.

```python
@mcp.tool()
def delete_step(workflow_id, step_id, confirm: bool = False):
    element = client.get_element(workflow_id, step_id)

    if not confirm:
        links = client.get_links(workflow_id)
        affected = []
        for link in links:
            if str(link.get("fromElement")) == str(step_id):
                affected.append(f"this step -> step {link.get('toElement')} will be broken")
            elif str(link.get("toElement")) == str(step_id):
                affected.append(f"step {link.get('fromElement')} -> this step will be broken")
        return DeleteStepResult(
            step_id=step_id, type=element.get("type"),
            label=element.get("name") or schema_registry.default_label(element.get("type")),
            needs_confirmation=True, affected_connections=affected,
            hint="Show this to the user. Call again with confirm=true only if they explicitly say to proceed.",
        )

    links = client.get_links(workflow_id)
    incident_link_ids = [l.get("link_id") for l in links
                         if str(l.get("fromElement")) == str(step_id)
                         or str(l.get("toElement")) == str(step_id)]
    link_deletes = [{"action": "delete", "linkID": lid, "data": {"link_id": lid}}
                    for lid in incident_link_ids]
    client.update_tree(
        workflow_id,
        elements=[{"action": "delete", "elementID": step_id, "data": {"element_id": step_id}}],
        links=link_deletes,
    )
    return DeleteStepResult(step_id=step_id, deleted=True)
```
The `confirm=True` branch's link cleanup exists because of a directly
measured finding: deleting an element via `updateTree` does **not**
cascade-delete its links (`probes/test_delete_impact.py`: built
start→A→B, deleted A, both links survived — one pointing *from* a step
that no longer existed, one pointing *to* one). Every link touching the
target step is deleted **in the same `update_tree` call** as the
element, so there's no window where the API could be interrupted between
"element gone" and "links cleaned up."

```python
@mcp.tool()
def publish_workflow(workflow_id, confirm: bool = False):
    combined = client.get_workflow_combined(workflow_id)
    if not confirm:
        ...
        health = graph.analyse(steps, conns)
        warnings = []
        if health["unreachable_steps"]: warnings.append(...)
        if health["dead_end_steps"]: warnings.append(...)
        if health["dangling_links"]: warnings.append(...)
        return PublishWorkflowResult(workflow_id=workflow_id, needs_confirmation=True,
                                     health_warnings=warnings, hint=...)
    client.publish_workflow(workflow_id)
    return PublishWorkflowResult(workflow_id=workflow_id, published=True)
```
The preview call doesn't just ask "are you sure" — it runs the **same**
`graph.analyse` health check `get_workflow` uses, and surfaces structural
problems (unreachable steps, dead ends, broken links) as warnings before
the workflow goes live. A workflow with warnings is still allowed to
publish — the point isn't to block it, it's to make sure the user hears
about a broken branch from the assistant before it goes live, rather than
discovering it from a submission that silently went nowhere.

```python
@mcp.tool()
def delete_workflow(workflow_id, confirm: bool = False, confirm_title: str = ""):
    meta = client.get_workflow(workflow_id)
    title = meta.get("title")

    if not confirm:
        return DeleteWorkflowResult(
            workflow_id=workflow_id, title=title, needs_confirmation=True,
            hint=f"Show the title '{title}' to the user. Call again with confirm=true "
                 f"and confirm_title='{title}' only if they explicitly confirm THIS workflow, by name.",
        )
    if confirm_title != title:
        return DeleteWorkflowResult(
            workflow_id=workflow_id, title=title,
            error=f"confirm_title ({confirm_title!r}) does not match this workflow's actual title ({title!r}). Not deleted.",
        )
    client.delete_workflow(workflow_id)
    return DeleteWorkflowResult(workflow_id=workflow_id, title=title, deleted=True)
```
The **only** tool in the project with a stricter guard than plain
`confirm=True` — it also requires `confirm_title` to exactly match the
workflow's real title. This exists because of a real near-miss during
testing: a real (non-disposable) workflow was deleted by mistake, by
picking the wrong entry from a numbered list that mixed real and
throwaway workflows. `confirm=True` alone only proves *some*
confirmation happened — it doesn't prove confirmation of the *right*
target. Forcing the title through means the model has to have surfaced
the actual name, not just an id, before this specific tool can act.
`delete_step` was deliberately left at the simpler pattern — a step is
recoverable by rebuilding it; an entire workflow is a much larger,
harder-to-recover-from loss, and earns the extra friction.

---

### `server.py`

```python
"""
Jotform Workflow MCP server.
...
Tool layers:
  1. discovery — list_step_types, get_step_schema
  2. reading   — list_workflows, get_workflow, get_step_details, list_forms, get_form_fields
  3. building  — create_workflow, add_step, connect_steps, update_step
  4. risky     — delete_step, publish_workflow, delete_workflow (confirm=True required to act)
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
```
30 lines, and every one earns its place. `load_dotenv()` runs **before**
the `from mcp_server.jotform_client import JotformClient` line — this
order is not cosmetic. `jotform_client.py` reads `JOTFORM_API_KEY` from
the environment at **module import time** (`BASE_URL = os.environ.get(...)`
at the top of that file), so if the imports happened first, the
environment variables wouldn't exist yet and the client would construct
with an empty key. The `# noqa: E402` comments silence the linter's "imports
should be at the top of the file" rule — broken here deliberately, for
exactly this reason. `client = JotformClient()` is constructed **once**,
at module scope, and passed into every `register()` call — this is why a
missing API key kills the server immediately on boot (see
`jotform_client.py`'s `__init__` above) rather than each tool failing
individually and confusingly later. Each layer's `register(mcp, client)`
adds its own tools to the same shared `mcp` instance; the layering exists
entirely at the file-organization level, `server.py` itself doesn't
enforce or even know about the layer boundaries.

---

## `tests/` — what's actually proven

37 tests total, run with `python -m pytest tests/ -q`, no network, no API
key, complete in under two seconds.

**`tests/test_graph.py`** (13 tests) — fixtures include the real 18-step
workflow this project first inspected, so a regression here shows up as a
change in a number that's already been manually verified once by eye.
Covers: correct orphan/dead-end counting on real data, end-points not
falsely flagged as dead ends, cycles not causing infinite loops, mixed
int/string step ids comparing correctly, the outcome-mapping logic
(matching `outcomes[].linkID` to connections, handling unconnected
branches, ignoring non-branching types like `workflow_split`), and the
dangling-link detector.

**`tests/test_tree_builder.py`** (24 tests) — id allocation, layout
fallback behavior, config validation (unknown fields dropped, enum
violations rejected, positioning/identity fields always stripped), the
exact measured link payload shape (pinned so a future edit to
`LINK_DEFAULTS` fails a test before it fails against the real API),
outcome resolution (case-insensitive match, unknown-outcome error,
already-connected error), the outcome-update payload preserving
untouched outcomes, and the default-outcome injection (binary decisions
get TRUE/FALSE, conditional branches get their default bucket,
non-branching types get nothing, caller-supplied outcomes are respected
not overwritten).

---

## `probes/` — one line each

These are not part of the product. They're how every claim in
`docs/gap-report.md` and `docs/decision-log.md` was actually established.
None of their code needs to be memorized — only what each one checked and
what it found.

### From this phase (Phase 1-4), reusable as regression checks

| Script | Checks | Found |
|---|---|---|
| `smoke_test.py` | All 14 tools, happy path, in ~10 seconds | Fast health check to run after any change |
| `inspect_links.py` | Raw fields on every link object | `labels` always empty; `fromPortName` is canvas geometry, not branch identity |
| `inspect_outcomes.py` | Whether `/combined` includes `outcomes` on elements | Yes — no extra call needed |
| `test_link_ports.py` / `test_link_ports2.py` | What fields a link write actually requires, and whether values are validated | `points` needs non-empty content (ignored); ports are unvalidated and self-correct; `type` is unvalidated and **not** corrected |
| `test_outcome_write.py` | Whether `outcomes[].linkID` can be set via `action:"update"`, with read-back | Yes, confirmed end-to-end |
| `test_write_path.py` | Create workflow / create element / create link / delete element, each read back | All confirmed working |
| `test_building_tools.py` | All of Layer 3 through `mcp.call_tool`, happy path and five deliberate failure modes | Everything passed; this run is what first exposed the default-outcomes bug |
| `test_delete_impact.py` | Does deleting an element cascade-delete its links? | **No** — links survive, pointing at nothing |
| `test_delete_workflow.py` | Does `DELETE /workflow/{id}` work? Also a cleanup utility (`--sweep-probes`) | Yes, confirmed and persistent |
| `test_noop_updatetree_effect.py` | Does an empty `updateTree` call change anything as a side effect? | No — safe |
| `test_set_trigger_form.py` / `inspect_trigger_binding.py` | Does `setResource` actually bind a trigger form? | **No** — confirmed silent no-op, diffed every field before/after |
| `test_publish_workflow.py` | Does `publish_workflow` actually work? | Endpoint accepts and returns `live: 1`, but `publishStatus`/`hasPublishedFlow` are not reliable confirmation signals |

### From Phase 0 (pre-dates this session, original exploration)

| Script | Checks |
|---|---|
| `client.py` | Shared harness — every Phase 0 probe logs through this to `probes/findings/*.jsonl` (currently empty — see note below) |
| `discover_from_official_sdk.py` | Pulled Jotform's official Python SDK source to get an authoritative endpoint list. Found: zero of 47 endpoints mention "workflow" — everything workflow-related in this project is empirically discovered, not sanctioned |
| `run_full_sweep.py` | Auto-sweeps every **GET** endpoint from that list; mutating ones are logged as skipped, never auto-fired |
| `run_public_api.py` / `run_internal_bff.py` | Confirms the two surfaces (`api.jotform.com` vs `www.jotform.com/API`) behave differently — the internal one is CSRF-blocked from outside a browser |
| `phase0_close_gaps.py` | The original script that found `list_workflows`, confirmed `/combined`, and confirmed element deletion |
| `explore_workflow_surface.py` / `explore_templates.py` | Reconnaissance sweeps of plausible-but-untried paths — mostly 404s, a few genuine finds |
| `inspect_workflow_elements.py` / `inspect_workflow_links.py` / `dump_specific_elements.py` | Dump raw, untruncated API responses to files for manual inspection |
| `compare_conditional_branch_types.py` | Compared `workflow_conditional_branch`'s full config against `workflow_binary_decision`'s |
| `fresh_workflow_and_type_test.py` | Set up a clean scratch workflow for testing element type persistence |
| `verify_pdf_reference.py` | Re-checked every claim in an internal reference doc against the live API, rather than trusting it as written |
| `test_elements_write.py` / `discover_element_schema.py` | Explored the **alternative** write path (`POST /workflow/{id}/elements`) that `build_branching_workflow.py` later used. **Note:** `probes/findings/` is empty — these specific scripts' success against the live API was likely never independently logged; treat as a plausible, HAR-backed hypothesis, not an equally-confirmed result next to this project's own `updateTree`-based findings |
| `build_workflow_from_scratch.py` / `build_polished_demo_workflow.py` / `build_branching_workflow.py` | End-to-end demo builds using the `POST /elements` path. Independently arrived at the same `type: "default-link"` rule and the same `outcomes[].linkID` branch mechanism this project found — good cross-confirmation, from a completely different exploration route |

---

## `docs/` — where the narrative lives

- **`gap-report.md`** — the living capability matrix: every endpoint used
  in this project, whether it's confirmed working, and exactly how it was
  verified. Read this to answer "can this product do X."
- **`decision-log.md`** — every non-obvious choice, dated, with the
  alternative considered and why it lost, including the ones that were
  *wrong* on the first attempt and how that was caught. Read this to
  answer "why does the code do it this way."

---

## Findings that shaped the design

The short version, if a mentor asks "what actually went wrong along the
way":

1. **Branch identity isn't on the link.** First guess (port names) was
   plausible and coincidentally correct on the one workflow tested, which
   would have shipped as a bug. The real answer — `outcomes[].linkID` on
   the deciding element — came from reading Jotform's own schema, not
   from testing harder on the same example.
2. **A link write needs four fields the API doesn't document**, and one
   of them (`type`) has to be a hardcoded constant because the API
   silently accepts and keeps a typo there, while the other three
   (`points`, port names) self-correct or don't matter.
3. **A schema's declared `default` isn't applied by the server.** An
   if/else created without explicit `outcomes` is permanently unwireable.
4. **Deletes don't cascade.** Deleting an element leaves its links behind,
   pointing at nothing.
5. **`setResource` is a silent no-op** on the public API — the most
   dangerous kind of failure, because it reports success.
6. **Boolean status flags lie.** `hasAnyWorkflow` and `hasPublishedFlow`
   were both observed `true` on records where the named condition was
   false. No boolean field from this API is trusted by name alone
   anymore — each needed an explicit true/false check before use.
7. **A near-miss deletion** (a real workflow deleted by picking the wrong
   entry from a list mixing real and disposable data) directly produced
   `delete_workflow`'s title-confirmation requirement.

---

## Running the server

```bash
cp .env.example .env      # fill in JOTFORM_API_KEY
pip install -r requirements.txt
python -m pytest tests/ -q          # 37 tests, no network, should all pass
python -m mcp_server.server         # boots the stdio server
```

Or via the launcher (handles working-directory issues MCP clients are
prone to):

```bash
./run_server.sh
```

To connect a local Claude Desktop: add an entry to
`claude_desktop_config.json` pointing `command` at `run_server.sh`'s
absolute path. Remote connectors (reached from Anthropic's cloud, not
your machine) are a separate deployment question — see gap-report.md's
note on the auth model changing for that case (one shared `.env` key
doesn't work once the server isn't yours alone to run).

## Current status

14 tools across 4 layers. 12 are fully confirmed end-to-end with
read-back verification. 1 (`publish_workflow`) works but its best
confirmation signal is still an open question. 1 known, confirmed,
permanent limitation (`create_workflow`'s `trigger_form_id` — the
underlying API call is a no-op; the tool now detects and reports this
rather than claiming false success).

No open item currently blocks using the server. Remaining gaps
(schema/UI-name coverage for a handful of step types, custom-named
conditional branches, real canvas auto-layout, the deployment/auth model
for a remote connector) are scope questions for what comes next, not
defects in what exists.