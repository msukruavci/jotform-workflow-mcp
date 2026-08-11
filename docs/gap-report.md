# Feasibility & Gap Report

Living document. Every row traces back to a probe run (script + logged
output, in `probes/findings/` for the Phase 0 harness or in each probe's
own printed output for Phase 1-4) — not to something observed once in
DevTools and never re-confirmed from outside a browser session.

Last updated: 2026-08-10 (Phase 4 close-out).

## Surfaces

| Surface | Base URL | Auth | Status |
|---|---|---|---|
| Public API | `api.jotform.com` | `apiKey` query param | Documented at `api.jotform.com/docs`. Confirmed reachable from outside a browser session. **47 endpoints confirmed via Jotform's own official Python SDK source** (see below) — no OpenAPI/Swagger spec exists; Jotform support has confirmed this in multiple threads. |
| Internal BFF | `www.jotform.com/API` | Browser session cookie (assumed — never seen an apiKey/Authorization header on it in captured traffic) | Not documented. Powers the workflow builder UI. Confirmed to reject cross-origin/non-session calls. Not built on — see decision log, 2026-08-05. |

## Discovery method: mining the official SDK instead of guessing paths

`probes/discover_from_official_sdk.py` downloads and parses
`github.com/jotform/jotform-api-python`'s source directly — every method
in that client maps 1:1 to a documented endpoint, so this gives the full,
authoritative list (47 endpoints: 43 GET, 4 mutating) without hand-copying
the docs page or blind-guessing paths. Output: `docs/public-api-surface.json`.

**Zero of the 47 endpoints mention "workflow" anywhere.** Workflow support
has no presence in Jotform's own official SDK. Every workflow endpoint used
in this project — `GET /workflow/{id}`, `/combined`, `/elements`, `/links`,
`updateTree`, `/publish`, and the DELETE verb on `/workflow/{id}` — was
found and verified empirically, not from any sanctioned source. Treat
workflow findings as one tier less certain than the forms/submissions
findings: they could change without notice, since Jotform hasn't committed
to them publicly.

## Capability matrix

### Forms and submissions

| Capability | Endpoint | Verified? | Result |
|---|---|---|---|
| List forms | `GET /user/forms` | ✅ 2026-08-05 | 200, real JSON, works with just apiKey |
| Get submissions | `GET /form/{id}/submissions` | ⚠️ inconclusive | 200, but tested form had 0 submissions. `workflowStatus` content still unverified — needs a form with real, workflow-triggered submissions. |
| List forms w/ workflow flag | `GET /user/forms?addWorkflow=1` | ⚠️ signal quality unverified | Adds `hasAnyWorkflow: true` per form, but was `true` for every form tested including unrelated ones. Not trusted as a signal. |

### Reading workflows

| Capability | Endpoint | Verified? | Result |
|---|---|---|---|
| List workflows | `GET /user/workflows` | ✅ 2026-08-07 | Found via HAR `x-raw-uri` hint, not documented anywhere. Returns `instance_summary.total` — run count — the clearest signal for "is this workflow actually live." |
| Read metadata + full tree in one call | `GET /workflow/{id}/combined?fetchEssentialElementProps=1` | ✅ 2026-08-07 | Metadata + elements + links in a single request. Verified to return every link `/links` returns (checked on two workflows: 8/8, 7/7) — the single-call optimisation is safe, no silent trimming. |
| Read one element's full config | `GET /workflow/{id}/elements/{id}` | ✅ | The list endpoint only summarizes; this returns full config including `outcomes`, `conditionTerms`, etc. |
| Branch identity (which link is TRUE/FALSE) | `outcomes[]` on the deciding element, not the link | ✅ 2026-08-07 | See "Branch identity" below — this took two wrong turns to find. |

### Writing workflows

| Capability | Endpoint | Verified? | Result |
|---|---|---|---|
| Create workflow | `POST /workflow` | ✅ | Start point must be included in the initial payload. |
| Create element | `PUT /workflow/{id}/updateTree`, `elements: [{action:"create", data:{type, ...}}]` | ✅ 2026-08-10, read-back confirmed | The write path this project standardized on. A sibling path, `POST /workflow/{id}/elements`, also exists in the client and appears in Jotform's own real browser traffic (`probes/build_branching_workflow.py`) but was never independently confirmed working from this project — see decision log. |
| Create link | `PUT /workflow/{id}/updateTree`, `links: [{action:"create", data:{...}}]` | ✅ 2026-08-10, eleven measured attempts | Needs `type`, `points`, `fromPortName`, `toPortName` all present. Exact rules below. |
| Set branch outcome (TRUE/FALSE/custom) | `updateTree`, `elements: [{action:"update", data:{outcomes:[...]}}]` | ✅ 2026-08-10, read-back confirmed | `get_element` shows the `linkID` stuck, not just a 200. |
| Update element config | `updateTree`, `elements: [{action:"update", ...}]` | ✅ | |
| Delete element | `updateTree`, `elements: [{action:"delete", ...}]` | ✅ 2026-08-06 (existence), 2026-08-10 (side effects) | Does **not** cascade-delete incident links — see below. |
| Delete workflow | `DELETE /workflow/{id}` | ✅ 2026-08-10 | Verified by absence from a subsequent `list_workflows`, not just the response code. Two other candidate shapes (`POST .../{id}` with `status: DELETED`, `PUT .../{id}/status`) were not needed once this one worked. |
| Set trigger form | `POST /workflow/{id}/setResource` | ❌ **CONFIRMED NOT WORKING, 2026-08-10** | Returns `true`, changes nothing. `probes/inspect_trigger_binding.py` diffed every field on the workflow metadata, the `/combined` workflow object, and the start point element, before and after — zero fields changed anywhere. Behaves like a silent no-op version of the internal-BFF's confirmed CSRF block, except it returns success instead of an error. `create_workflow`'s `trigger_form_id` now verifies the bind by reading the start point back and reports an explicit error if it didn't take, rather than trusting the `true` response. |
| Publish workflow | `POST /workflow/{id}/publish` | ⚠️ **partially confirmed, 2026-08-10** | 200, with a structured response containing `live: 1`. But `workflow.publishStatus` stayed `"DRAFT"` and `hasPublishedFlow` was already `true` on a brand-new, never-published workflow — neither field is a trustworthy "is this published" signal (see below). The call likely works; the field this project checked to confirm it does not. |

### The measured link-write rules

Writing a link needs four fields beyond the two endpoints. Rules, measured
across eleven attempts (`probes/test_link_ports.py`, `test_link_ports2.py`):

| field | rule |
|---|---|
| `points` | must be non-empty; contents ignored. `[]` is rejected as missing — PHP `empty()` semantics. Real workflows carry junk like `[{"a":"1"}]` or `[{"1":2}]`, stored verbatim. |
| `fromPortName` / `toPortName` | presence required, **value not validated**. Nonsense names are accepted and silently rewritten to the canonical pair. Valid names are kept as sent. Empty strings are rejected. |
| `type` | presence required, value not validated, and **not corrected** — `banana-link` persisted unchanged. Always send `default-link`; this field must never be caller-supplied. |

Working payload:

    {"type": "default-link", "points": [{"a": "1"}],
     "fromPortName": "DYNAMIC_BOTTOM_1_Out", "toPortName": "DYNAMIC_TOP_1_In"}

Consequence: the feared "port model per step type" mostly evaporates — the
server computes real ports itself. This also proves ports cannot carry
branch meaning: the server has no way to know which branch a link
represents from the link alone.

**Not yet checked:** whether the server assigns visually-sensible ports
when one step has *two* outgoing links (an if/else). `probes/build_branching_workflow.py`
(a separate, HAR-derived recipe, not run through this project's own harness)
uses a *different* port for each of the two branches — suggesting the two
exits should get different ports for a clean-looking canvas even though
both work functionally either way. Cosmetic, not correctness — folded into
gap 5 (canvas layout) below.

### Branch identity: outcomes, not links

Took two wrong turns before landing correctly (see decision log,
2026-08-07):

1. First guess: branch label lives on the link, in some field we hadn't
   found the name of yet.
2. Looked plausible: `fromPortName` ("RIGHT_MIDDLE_Out" vs "DYNAMIC_TOP_1_Out")
   correlated with TRUE/FALSE on the one workflow tested. Wrong — that's
   canvas geometry, not branch identity, and the correlation was
   coincidental to that node's layout.
3. Correct: the label lives on the **deciding element**:

       workflow_binary_decision.outcomes = [
         {"outcomeID": 1, "conditionValue": "TRUE",  "linkID": 2},
         {"outcomeID": 2, "conditionValue": "FALSE", "linkID": 3}
       ]

   `linkID` maps to a connection's `link_id`. `workflow_conditional_branch`
   uses the same shape with custom names. `workflow_split` has no
   `outcomes` at all — its paths are equivalent, so a missing label there
   is not an error.

`/combined` already returns `outcomes` — no extra call needed, `get_workflow`
stays a single request. Verified: `probes/inspect_links.py`,
`probes/inspect_outcomes.py`, `probes/test_outcome_write.py`. Pinned by
`tests/test_graph.py`, `tests/test_tree_builder.py`.

**A second, separate bug this exposed:** a fresh `workflow_binary_decision`
element created via `updateTree` comes back with **no** `outcomes` at all
unless the caller supplies them explicitly — the JSON Schema's `default`
value (TRUE/FALSE) describes what a client should display, the server does
not apply it on create. An if/else created without this is permanently
unwireable. Fixed: `tree_builder.build_element_create` injects the schema
default for branching types at creation time. Pinned by
`tests/test_tree_builder.py`.

**Not solved:** `workflow_conditional_branch`'s default is a single
catch-all "OTHER" bucket. Custom-named branches with their own condition
terms (e.g. "Refund", "Complaint") aren't buildable through `add_step` —
that needs a config surface for defining outcomes with condition terms that
`get_step_schema` doesn't currently expose in a model-usable way. A user
wanting custom branches has to build the step in Jotform's UI and use this
server to read/connect around it.

### Delete does not cascade

Deleting an element does **not** remove its incident links — confirmed by
building start→A→B, deleting A, and finding both links still present,
pointing at a step that no longer exists (`probes/test_delete_impact.py`,
2026-08-10). `delete_step` now explicitly deletes every link touching the
target step in the same `updateTree` call as the element. `graph.py`'s
`health.dangling_links` check stays regardless, as a read-side safety net
for anything this doesn't catch (e.g. links from a step deleted some other
way, or through a future tool that forgets this rule).

## Two candidate write paths — one standardized on

Two ways to create an element were explored across this project's history:

1. `POST /workflow/{id}/elements` (bare create) + a follow-up `updateTree`
   to configure it. `jotform_client.create_element()` wraps this. Appears
   in `probes/build_branching_workflow.py`, derived from real browser
   traffic (HAR capture) — but `probes/findings/` is empty, so this
   specific script's success against the live API from outside a browser
   session was never independently logged by this project's own harness.
   Treat as a plausible, HAR-backed hypothesis, not a confirmed result.
2. `updateTree` alone, `elements: [{action:"create", data:{...full config...}}]`
   in one call. This is what `tree_builder.py` and everything in Phases
   1-4 is built on, and every claim about it traces to a probe with a
   read-back verification step.

Phase 1-4 standardized on (2), single-call. `create_element()` is kept in
`jotform_client.py`, unused — a viable alternative, not dead weight to
delete, in case a future need (e.g. wanting to know an element's assigned
id before configuring it) makes the two-call shape preferable.

## Schema coverage gaps

`workflow_payment_verification` appears in a real account and has no
schema in `schemas/workflow_all_schemas.json`. Listed with
`schema_available: false` rather than hidden — hiding it would tell the
model a real step type does not exist.

Builder-UI elements with no confirmed type mapping: **Approve & Sign, Team
Approval, Flow Report, PDF** (`schema_registry.UNMAPPED_UI_ELEMENTS`).

Two entries in `UI_NAMES` are marked UNCONFIRMED: `workflow_payment_verification`
→ "Payment Form", `workflow_reminder_email` → "Scheduled Email".

**How to close:** add one of each element in the builder UI, read the
workflow back through `get_workflow`, note the type that appears.

## Canvas positioning — no real strategy

`x`/`y` are hidden from the model on purpose (handled server-side), but
what computes them (`tree_builder.compute_position`) is a straight
below-the-anchor placement with no collision detection against the rest of
the canvas. Anything Phase 3/4 creates in a workflow that already has
content elsewhere may overlap it visually. Also unresolved: whether the two
branches of an if/else should be written with different `fromPortName`
values for a cleaner-looking canvas (see link-write rules above) —
functionally unnecessary, cosmetically possibly worth doing.

## Boolean status flags are not trustworthy signals — a pattern, not a one-off

Two independent instances now: `hasAnyWorkflow` (Phase 0) returned `true`
for every form tested, including ones with no workflow. `hasPublishedFlow`
(Phase 4) returned `true` for a workflow that had never been published.
Neither tracks what its name claims. Treat any Jotform boolean metadata
flag as unverified until specifically checked against both a true and a
false case — do not infer meaning from the field name alone, here or in
future exploration.

## Open, unresolved as of 2026-08-10

- [x] ~~`test_noop_updatetree_effect.py`~~ → **Safe.** An empty `updateTree`
      call does not change an element's type or anything else. Confirmed
      2026-08-10: read type before, sent `{"elements":[],"links":[]}`,
      read type after — unchanged. `connect_steps`'s `action:"update"` on
      every call carries no hidden side-effect risk.
- [x] ~~`setResource`~~ → **Confirmed not working on the public API.**
      Silent no-op, not an error — the most misleading kind of failure.
      `create_workflow` now verifies the bind by reading the start point
      back rather than trusting the response. Binding a trigger form
      currently requires the Jotform builder UI; there is no known public
      API path. This is a real, evidenced scope limitation for the
      product — worth stating plainly in the handover doc, not just here.
- [ ] `publish_workflow` — endpoint accepts the call and returns
      `live: 1`, but no reliable read-side confirmation yet (see boolean
      flags note above). A follow-up probe should check the publish
      response object's own fields (`published_at`, `live`) on a workflow
      re-fetched after a short delay, rather than `workflow.publishStatus`.
- [ ] Custom-named conditional branches (`workflow_conditional_branch` with
      real outcome names, not just "OTHER") — no buildable path yet.
- [ ] Schema/UI name gaps above.
- [ ] Canvas layout collision handling.
- [ ] `workflowStatus` on submissions — content still unknown, needs a form
      with real workflow-triggered submissions.
- [ ] `hasAnyWorkflow` filter signal quality — untrusted, always seen `true`.
- [ ] Deployment / auth model: works today as local stdio with one shared
      `.env` API key. A remote MCP connector is reached from Anthropic's
      cloud and needs to be publicly reachable with per-user Jotform keys —
      not evaluated at all yet, out of scope for the six-week build but
      real for anything beyond it.
- [ ] Minor: `get_folders` (`/user/folders`) returns 400, "deprecated, use
      Label endpoints" — check the Label endpoints if folder/label
      organization ever matters to the product.

## Resolved history (Phase 0, 2026-08-05/06 — kept for the record)

- `api.jotform.com/workflow/{id}` returns 200 with real metadata,
  contradicting a stale 2023 support thread claiming it was inaccessible.
- `POST api.jotform.com/workflow/{id}` (metadata write) works — this
  reversed an earlier, premature "workflow writes are blocked" conclusion
  that had only been tested against the internal BFF, not the public
  surface. See decision log, 2026-08-06, for the full account of that
  reversal and its cost (~1 day of wrong scoping assumption).
- `GET /workflow/{id}/elements` returns real node data on the public
  surface, undocumented but working.
- Internal BFF (`www.jotform.com/API`) confirmed CSRF-blocked for
  `setResource`, `updateTree`, and `publish` from outside a browser
  session — not pursued further, by design (see decision log).

## Blank canvas on API-built workflows — CLOSED 2026-08-10

A workflow built entirely through this project's tools opened to a blank,
empty canvas in Jotform's own builder UI — no error, no toast, nothing
rendered. Every step existed server-side (`get_workflow_combined`
confirmed it), so the failure was client-side, in Jotform's own renderer.

`probes/compare_element_shapes.py` diffed our created elements against a
real, working reference workflow's elements of the same types. Result:
Jotform's server auto-fills most fields' schema defaults on create
(`subTypeText`, `content`, `fromName`, and a dozen others matched with no
diff) — but not a specific handful, all arrays a step's renderer would
plausibly iterate over:

| step type | field(s) missing | schema default |
|---|---|---|
| `workflow_send_email` | `to` | `[]` |
| `workflow_binary_decision` | `conditionTerms` | `[]` |
| `workflow_assign_task` | `assignee` | `""` |
| `workflow_assign_task` | `outcomes` (its own meaning — the completion button, unrelated to branch outcomes) | `[{"text": "Complete", ...}]` |

Consistent with an uncaught client-side render exception (`undefined`
where an array was expected) swallowed by an error boundary that renders
nothing, rather than a network or data-loading failure.

This is the same shape of bug as gap 7 (branching `outcomes` not
auto-defaulted) — turned out to be one instance of it, not a separate
issue. Fixed by generalizing: `schema_registry.get_field_defaults` now
returns every field with a schema-declared default for a given step
type, and `tree_builder.build_element_create` injects any of them the
caller didn't supply. `name` is deliberately excluded — the reference
workflow's elements had no `name` key at all and rendered fine, so
injecting a generic default there would be a needless deviation from
what a real element looks like.

Also fixed: `create_workflow`'s hardcoded start-point payload was missing
`subType: "workflow_start_point_submission"`, present on every real
start point checked. Unlikely to be the render-blocking field on its own
(a missing string is less likely to crash a `.map()` than a missing
array), but corrected for consistency now that it's known.

**Not yet re-verified visually.** The fix changes what *future* `add_step`
calls send; it does not retroactively repair elements already created
before this fix (like the workflow that surfaced the bug). Delete that
one and have a fresh build go through the corrected code, then confirm
in the Jotform builder UI that it actually renders — the diff proves the
right fields are now present, not yet that presence alone fixes the
canvas. Pinned by six new tests in `tests/test_tree_builder.py`; not
proven live yet.