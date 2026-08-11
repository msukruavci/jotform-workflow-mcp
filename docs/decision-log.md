# Decision Log

Non-obvious choices, alternatives considered, and why. Add an entry
whenever a decision would look arbitrary to someone reading the code cold.

---

### 2026-08-05 — Split "public API" and "internal BFF" into separate concepts everywhere

**Decision:** Treat `api.jotform.com` and `www.jotform.com/API` as two
distinct surfaces with separate probe scripts, separate Bruno folders, and
separate rows in the gap report — never grouped together as "the Jotform
API."

**Why:** They have fundamentally different auth models and support
guarantees. Collapsing them into one mental category made early
exploration slower (kept re-testing the wrong assumption about auth) and
would make the final gap report misleading — "the API supports X" is only
true for one of the two.

**Alternative considered:** Treat everything found via DevTools as fair
game and sort out documented-vs-not later. Rejected — the "public surface
only" ground rule means the internal BFF findings are diagnostic (they
tell us what Jotform's own UI does) but not something to build on.

---

### 2026-08-05 — MCP server does not implement workflow write tools yet

**Decision:** `mcp_server/tools/workflows.py` exists as an empty
placeholder rather than being left out entirely or half-implemented
against the internal BFF.

**Why:** Makes the gap visible in the code path a future engineer would
actually read, not just in a doc they might skip. Also stops the tool
surface from silently growing to include something we can't reliably
support.

**Superseded 2026-08-10:** the write path was found and confirmed (see
below); this placeholder and its sibling stub tool modules
(`submissions.py`, `forms.py`, referencing client methods that never
existed) were deleted once `tools/building.py` replaced them for real.

---

### 2026-08-05 — Don't trust community/support answers about API behavior without re-testing

**Decision:** Treat forum/support-thread claims about what an endpoint
does as a hypothesis to test, not a fact to build on — log a probe result
even when it just confirms what a thread already said.

**Why:** A 2023 Jotform support thread stated `api.jotform.com/workflow/{id}`
was inaccessible to normal accounts. A same-day probe against the current
API showed it returns 200 with real data. The thread wasn't wrong when it
was written — the platform just moved. Anything more than a few months
old should be re-verified, not cited as-is in the gap report.

**Consequence:** upgraded "Read workflow detail" from "unverified,
probably locked" to "confirmed working, metadata only" in gap-report.md
based on one probe run, not on the old thread.

---

### 2026-08-06 — Reversed the "workflow writes are blocked" conclusion for the public-api surface

**What happened:** 2026-08-05's gap report concluded workflow authoring
wasn't achievable on the public surface, based on the internal BFF
(`www.jotform.com/API`) being CSRF-blocked. We hadn't yet tried the same
operations against `api.jotform.com` directly. A day later: `POST
api.jotform.com/workflow/{id}` works (200), and `GET .../elements`
returns real node data neither endpoint's internal-bff counterpart would
give us from outside a session.

**Lesson for how this project runs:** a negative result on one surface
(internal-bff) doesn't transfer to a sibling surface (public-api) that
happens to expose a similarly-named path. Two endpoints with the same
verb+resource name on different hosts are not the same endpoint — don't
let a failure on one stand in for an untested guess about the other. This
cost us roughly a day of treating "read-only" as the likely v1 scope when
it wasn't yet settled.

**Consequence:** gap-report.md's headline finding and capability matrix
were rewritten rather than patched.

---

### 2026-08-05 — Mine the official SDK instead of brute-forcing or hand-copying docs

**Decision:** Built `probes/discover_from_official_sdk.py` to parse
Jotform's own `jotform-api-python` client source for the canonical
endpoint list, rather than (a) hand-transcribing `api.jotform.com/docs`,
or (b) dictionary/brute-force guessing paths against the live API.

**Why:** The official client's methods map 1:1 to real, sanctioned
endpoints. Brute-forcing was rejected outright: firing a dictionary of
guessed paths at a third party's production API is a bad look even
against your own account, and the brief's "public surface only, build the
way any external developer would" rules it out anyway.

**Consequence, and a real finding:** zero of the 47 extracted endpoints
mention "workflow." Every workflow-related finding in this project is
empirically discovered, not sanctioned.

---

### 2026-08-05 — Chose Python for the server skeleton

**Decision:** Python over TypeScript for the initial MCP server.

**Why:** Lowest ceremony to get a local server running and testable with
the MCP Inspector today. Not a strong opinion — `probes/` and `docs/`
stay language-agnostic regardless.

**Update, 2026-08-07:** the MCP Python SDK in use is 2.0
(`mcp.server.MCPServer`), not the 1.x `FastMCP` API the earliest server
skeleton assumed. `.tool()` decorator usage is unchanged; only the import
path and class name moved. Caught by a boot-test import error, not by
reading changelogs — worth remembering that "the SDK works this way" is
itself something to verify against the installed version, not assume from
memory or older examples.

---

### 2026-08-07 — Flattened `allOf` in schema simplification

**Decision:** `schema_registry._flatten_all_of` merges `allOf` branches
before simplifying a property, rather than leaving them as-is.

**Why:** Jotform hides both the type and the description of every rich
field (`to`, `cc`, condition terms) inside `allOf`. Unflattened, the model
saw `{"name": "to", "type": "any"}` for an email step's recipient list —
no hint of what to send. Measured impact across all 36 schemas: 14 fields
collapsed to `"any"`, 90 had no description, before this fix. After: 2 and
78 respectively (the remainder are genuinely untyped/undocumented in
Jotform's own schema, not something flattening can fix).

---

### 2026-08-07 — Surfaced link outcome instead of the port name that looked right

**What happened:** Building `get_workflow`'s branch-labelling, the first
candidate for "which field says TRUE vs FALSE" was `fromPortName` — it
correlated with the branch on the one test workflow available at the time
(`RIGHT_MIDDLE_Out` → TRUE, `DYNAMIC_TOP_1_Out` → FALSE). This would have
shipped as correct; it passed the only check it was given.

**Why it was wrong:** the correlation was an accident of that node's
canvas layout, not a property of the field. Confirmed by reading the raw
JSON Schema for `workflow_binary_decision`: branch identity is declared
there as `outcomes[] = {conditionValue, linkID}`, on the *element*, not
the link. `probes/inspect_outcomes.py` confirmed `/combined` already
returns it.

**Lesson:** a field that correlates with the right answer on one example
is not the same as a field that *means* the right answer. The fix here
was reading the schema Jotform actually publishes for the element, not
pattern-matching harder on the one workflow already open. See
`gap-report.md`, "Branch identity" for the full three-attempt account.

**Consequence:** `Connection.outcome` now comes from the deciding
element's `outcomes` array; `Connection.from_port` is kept separately,
un-surfaced to the model, purely because writing a link back later
requires *some* port value (see 2026-08-10 entry below) — it carries no
meaning and must never be treated as one again.

---

### 2026-08-07 — `graph.py` as a pure, network-free module

**Decision:** Reachability/dead-end/unknown-type analysis lives in its own
module with no imports beyond the standard library, taking and returning
plain dicts.

**Why:** It's the one part of the reading layer provable without an API
key or a network call. `tests/test_graph.py` runs in milliseconds against
a fixture taken from the real 18-step test workflow, and catches
regressions the same day they're introduced rather than the next time
someone happens to eyeball a `get_workflow` result by hand.

---

### 2026-08-07 — Pydantic return models instead of `-> dict`

**Decision:** Every tool declares a Pydantic model as its return type
(`mcp_server/models.py`) rather than `-> dict` or `-> list[dict]`.

**Why:** Found via MCP Inspector: a tool typed `-> dict` gives the SDK
nothing to build an `outputSchema` from, so `get_workflow`,
`get_step_details`, and `get_step_schema` were shipping their richest
data as an unstructured text blob instead of structured content the
client could parse. `list_*` tools (returning `list[dict]`) did get a
schema, inconsistently — the fix needed to cover both shapes.

---

### 2026-08-10 — Standardized element/link writes on `updateTree`, not `POST /elements`

**Decision:** `tree_builder.py` and every building/risky tool write
through `PUT /workflow/{id}/updateTree` exclusively. `create_element()`
(`POST /workflow/{id}/elements`) stays in `jotform_client.py`, unused, not
deleted.

**Why:** Two candidate write paths surfaced across this project's history
— `updateTree` (found and rigorously probe-verified, including read-back,
in this project) and `POST /elements` + follow-up config (derived from
real browser traffic in `probes/build_branching_workflow.py`, predating
this phase, but never independently run through this project's own
verification harness — `probes/findings/` is empty for it). Building on
the path with the stronger evidence chain, not the one that merely looks
plausible from a HAR capture, matches the project's own standing rule
(gap-report.md's header: every claim traces to a logged run).

**Alternative considered:** adopt the two-call `POST /elements` pattern
since it was already partly explored. Deferred, not rejected — if a
future need makes knowing an element's id before configuring it valuable,
`create_element()` is already there.

---

### 2026-08-10 — Link write payload is one constant, `type` is never caller-supplied

**Decision:** `tree_builder.LINK_DEFAULTS` is a fixed dict — `type`,
`points`, `fromPortName`, `toPortName` — merged into every link write.
None of these four fields can be influenced by tool input.

**Why:** Measured across eleven attempts (`test_link_ports.py`,
`test_link_ports2.py`): `points` must be non-empty but its contents are
ignored (`[]` is rejected — PHP `empty()` semantics); port names are
required but unvalidated *and silently rewritten* to the real value by
the server; `type` is required, unvalidated, and **not corrected** — a
typo there (`banana-link`) persists forever with no error at write time
and no visible symptom until something tries to render or traverse that
link. That asymmetry (ports self-heal, type doesn't) is why `type` is a
constant and the others were worth testing but didn't need to be.

---

### 2026-08-10 — Inject default `outcomes` on branching-type create

**What happened:** Every `connect_steps` call against a freshly created
`workflow_binary_decision` failed with `Available outcomes: []`
(`probes/test_building_tools.py`). The JSON Schema lists a `default` of
TRUE/FALSE `outcomes` — but that default describes what a UI *client*
should pre-populate, and the server does not apply it server-side on
`create`. An if/else built through `add_step` alone was permanently
unwireable.

**Fix:** `tree_builder.build_element_create` reads the schema's `outcomes`
default and injects it at creation time for any type in
`schema_registry.BRANCHING_TYPES`, unless the caller already supplied one.

**Why this belongs in `tree_builder`, not `add_step`'s tool code:** it's
plumbing analogous to port names — something the API needs to function
that a model has no reason to know about. Pinned by four tests in
`tests/test_tree_builder.py`, including one confirming caller-supplied
outcomes are respected, not overwritten.

---

### 2026-08-10 — Two-call confirm pattern for delete/publish, not a single flag

**Decision:** `delete_step`, `publish_workflow`, `delete_workflow` all
require two separate tool calls: one with `confirm=false` (default) that
changes nothing and returns a preview, one with `confirm=true` that acts.

**Why:** MCP tools are synchronous request/response — there's no channel
to pause mid-call and wait for a human answer. Forcing two calls means the
model must have already shown the preview and received an explicit
go-ahead in the conversation before `confirm=true` can mean anything.
Mirrors the same two-call pattern this assistant's own runtime uses for
ending a conversation.

**Escalated for `delete_workflow` specifically, same day:** during manual
testing, a real workflow (not a disposable probe artifact) was deleted by
picking the wrong index from a numbered list that mixed real and
throwaway workflows. `confirm=true` alone doesn't protect against a model
(or a person) confidently confirming the *wrong* target — it only proves
*some* confirmation happened. `delete_workflow` now also requires
`confirm_title` to exactly match the workflow's current title, forcing
the model to have surfaced the actual name, not just an id, before this
tool can act. `delete_step` was left at plain `confirm=true` — a step is
recoverable by rebuilding it; a whole workflow is a much larger loss.

---

### 2026-08-10 — `delete_step` explicitly deletes incident links; `graph.py` checks for dangling ones regardless

**What happened:** Assumed deleting an element would cascade-delete its
links, the way many systems do. Tested directly
(`probes/test_delete_impact.py`): built start→A→B, deleted A, both links
survived — one now pointing *from* a step that no longer existed, one
pointing *to* one.

**Fix, two layers:** `delete_step` now finds every link touching the
target step and deletes them in the same `updateTree` call as the
element. Independently, `graph.analyse` gained a `dangling_links` check
(a link whose `from_step` or `to_step` isn't in the current step list) —
added as a read-side safety net *before* the write-side fix was confirmed
necessary, and kept afterward, since it also catches any future write
path that makes the same wrong assumption this one did.

---

### 2026-08-10 — Closed the `updateTree` no-op safety question; found a second untrustworthy boolean flag

**noop_updatetree:** `connect_steps` sends `action:"update"` to the source
element on every call. Whether `updateTree` has any side effect on fields
it wasn't asked to touch was an open question directly relevant to that
tool's safety. Tested directly: read an element's type, sent an empty
`updateTree`, read the type again — unchanged. Closed; no mitigation
needed.

**Boolean flags:** Testing `publish_workflow`, `hasPublishedFlow` was
`true` on a workflow that had never been published — useless as a signal.
This is the same shape as Phase 0's `hasAnyWorkflow` (`true` for every
form tested, including unrelated ones). Now treated as a pattern, not a
coincidence: **no Jotform boolean metadata field is trusted based on its
name alone** — each one needs an explicit true-case/false-case check
before code relies on it. `publish_workflow`'s tool result still reports
`published=True` from a non-erroring call, which is presently the best
available signal, not an independently confirmed one — noted in the tool
and in gap-report.md rather than silently assumed solid.

**Also observed:** a transient `401 Unauthorized` on `create_workflow`
during `test_set_trigger_form.py`, with the identical call succeeding
seconds later in a separate probe run. Likely rate-limiting from this
project's own probe traffic — this session alone created and deleted well
over a dozen throwaway workflows. Worth knowing for the handover doc: a
single 401 on a call that's worked repeatedly elsewhere in this project is
not evidence the endpoint is broken, and retrying is reasonable before
concluding otherwise.

---

### 2026-08-10 — setResource confirmed as a silent no-op; create_workflow now verifies its own write

**What happened:** `probes/inspect_trigger_binding.py` diffed every field
on the workflow's metadata, its `/combined` representation, and its start
point element, before and after calling `setResource` with a real form
id. The call returned `true`. Nothing, anywhere, changed.

**Why this is worse than an error:** a rejected call is a clear signal —
build around it, tell the user it can't be done. A call that returns
success and does nothing produces a tool that *lies*, quietly, unless
something downstream happens to check. `create_workflow`'s
`trigger_form_id` parameter was exactly that tool until today: it called
`set_trigger_form`, got no exception, and reported success.

**Fix:** `create_workflow` now reads the start point element back after
calling `set_trigger_form` and compares `resourceID` to what was sent. If
it doesn't match, the tool returns an explicit error saying the bind is a
known public-API limitation and the user needs to do it in the Jotform
builder — the workflow itself is still created and usable, just not
bound to a trigger form automatically.

**Broader lesson, consistent with the two boolean-flag findings above:** a
response code and a response body are not the same as a verified effect.
Every write tool in this project that matters (`add_step`, `connect_steps`,
`delete_step`, `delete_workflow`) already reads state back to confirm —
`create_workflow`'s trigger-form path was the one exception, added early
and never revisited until this probe. Treat "we read it back and it
matched" as the bar for every future write tool, no exceptions carried
forward out of convenience.

---

### 2026-08-10 — Generalized default-field injection after a real render failure

**What happened:** A workflow built entirely through this project's
tools opened to a blank canvas in Jotform's builder — data existed
server-side, nothing rendered client-side, no error shown.
`probes/compare_element_shapes.py` diffed our elements against a real,
working reference and found the server does not auto-default a specific
handful of array fields (`to`, `conditionTerms`, `assignee`, task
`outcomes`) that it does default for everything else. The most likely
mechanism: a renderer iterating (`.map`) over one of these where it's
completely absent rather than an empty array, throwing, and an error
boundary rendering nothing instead of surfacing the crash.

**Decision:** rather than patch in exactly these four fields,
generalized the existing branching-`outcomes`-only special case
(2026-08-10, earlier entry) into `schema_registry.get_field_defaults` —
every field on a step type with a schema-declared default gets injected
if the caller didn't supply one, for every step type, not just the ones
already known to be affected.

**Why generalize rather than special-case the four known fields:** the
four were found by diffing exactly one reference workflow against exactly
one broken one. A narrower fix protects against the specific bug already
found; it says nothing about whether the same failure mode exists in
step types never diffed (`workflow_approval`, `workflow_webhook`, the 13
AI types, ...). The general mechanism costs nothing extra for fields the
server already defaults correctly (sending its own default value back is
redundant, not harmful) and automatically covers the same bug pattern
anywhere else it might exist, known or not yet found.

**Deliberate exception: `name`.** The schema declares defaults for it too
("Email", "Task", ...), but the reference workflow's elements had no
`name` key at all and rendered correctly — real elements only carry one
if their creator actually renamed the step. Auto-injecting a generic name
would be a needless, non-representative deviation from what a real
Jotform element looks like, unlike the other fields where "present but
empty" is exactly the real, working shape.

**Not yet closed:** the fix is proven against the schema and pinned by
tests, not yet confirmed to actually resolve the blank canvas visually —
that requires deleting the broken test workflow, rebuilding it with the
corrected code, and opening it in the Jotform UI. See gap-report.md.