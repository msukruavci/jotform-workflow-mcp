# Jotform Workflow MCP Feature Test Prompts

Use these prompts in ChatGPT with the Jotform Workflow MCP plugin enabled.
All prompts are intentionally written in English because the plugin should
default to English unless the user asks for another language.

## Part 1: Step-by-Step Test Set

### Test 1: Tool Discovery and Schema Awareness

Prompt:

```text
List the workflow step types you support. Then show me the schema for Email, Task, Approval, Conditional Branch, Wait for Duration, PDF, Team Approval, Approve & Sign, and Verify Payment if they are supported. Do not create anything yet.
```

Expected:

```text
- It should call list_step_types.
- It should call get_step_schema for supported requested step types.
- It should mention canonical type/subtype for UI variants such as PDF, Team Approval, Approve & Sign, and Wait for Duration.
- It should not create a workflow or any step.
```

Unexpected:

```text
- It creates a workflow or step without being asked.
- It claims unsupported fields without checking schema.
- It ignores subtype/canonical type information.
```

---

### Test 2: New Workflow Must Start With a Form Strategy

Prompt:

```text
Create a new workflow for candidate screening.
```

Expected:

```text
- It should not directly call create_workflow without a trigger form.
- It should ask whether I want to use an existing form or create a new AI form.
- It should keep the question short.
```

Unexpected:

```text
- It creates a forms-free workflow.
- It calls list_forms before asking for form strategy.
- It creates a placeholder trigger.
```

Follow-up prompt if it asks for form strategy:

```text
Create a new AI form. The form should collect Full Name, Email Address, Phone Number, Position Applied For, Years of Experience, Portfolio URL, and Expected Salary. Make the key contact and application fields required. Name the workflow "Candidate Screening Workflow - MCP Feature Test".
```

Expected:

```text
- It should call build_workflow_bulk with form_prompt.
- The language should be English by default.
- It should return a workflow builder link in this format:
  https://www.jotform.com/workflow/{workflow_id}/build
- It should return a trigger form builder link in this format:
  https://www.jotform.com/build/{form_id}
- It should include the created form fields/questions.
```

Unexpected:

```text
- It creates only a form but not a workflow.
- It creates a workflow but does not bind the form as trigger.
- It does not show direct builder links.
```

---

### Test 3: Add Application Receipt Email With Real Form Fields

Prompt:

```text
Add an email step after the On Submission trigger. Name it "Application received". Send it to the applicant using the real Email Address field from the trigger form. Subject: "We received your application". Body: "Hi {Full Name}, thank you for applying for {Position Applied For}. We received your application and will review it soon." Use real form field references instead of plain text placeholders.
```

Expected:

```text
- It should use trigger_form_fields from get_workflow / build_workflow_bulk if needed.
- It should add a workflow_send_email step.
- It should normalize recipient to the real Email Address field.
- It should normalize content tokens to real field tokens such as {q2_fullname0}.
- It should wrap plain text email body as HTML.
- It should connect the step after the trigger if the trigger has no outgoing link.
```

Unexpected:

```text
- It sends to plain text "Email Address" instead of the real form email field.
- It leaves {Full Name} as raw text instead of using the field token.
- It creates a disconnected email step when it could safely connect it.
```

---

### Test 4: Task Guardrail Should Ask for Required Details

Prompt:

```text
Add an HR review task after the application received email.
```

Expected:

```text
- It should not create an empty task.
- It should ask for the assignee, short task description, and outcomes.
- The question should be concise.
```

Unexpected:

```text
- It creates a task with no assignee.
- It creates a task with no description or outcomes.
- It guesses the assignee.
```

Follow-up prompt:

```text
Assign the HR review task to hr@example.com. Task description: "Review the candidate application, years of experience, portfolio URL, and expected salary. Choose the next action." Use these outcomes: Proceed to Interview, Reject, Request More Information.
```

Expected:

```text
- It should add a workflow_assign_task step.
- It should use a builder-compatible fixed email assignee object.
- It should create all three outcomes.
- It should connect the task after Application received if that step has no outgoing connection.
```

Unexpected:

```text
- It says task outcomes cannot branch.
- It replaces the task with an approval without asking.
- It creates duplicated tasks.
```

---

### Test 5: Branch Outcome Connections

Prompt:

```text
Create three applicant email steps for the HR review outcomes:

1. For "Proceed to Interview": name "Interview invitation", send to the applicant Email Address field, subject "Interview invitation", body "Hi {Full Name}, we would like to invite you to an interview for {Position Applied For}."
2. For "Reject": name "Application update", send to the applicant Email Address field, subject "Application update", body "Hi {Full Name}, thank you for your interest. We will not move forward at this time."
3. For "Request More Information": name "More information needed", send to the applicant Email Address field, subject "More information needed", body "Hi {Full Name}, we need a little more information before continuing your application."

Connect each email to the matching HR review outcome. Use real form field references.
```

Expected:

```text
- It should create or reuse three email steps.
- It should wire each one through build_workflow_bulk using the exact outcome text.
- It should update both links[] and the HR task outcomes[].linkID mapping.
- get_workflow should show connections with outcomes:
  Proceed to Interview
  Reject
  Request More Information
```

Unexpected:

```text
- Email steps remain disconnected.
- Link labels appear but task outcomes do not get linkID values.
- It reports INVALID_ARGUMENT for valid task outcome routing.
- It creates duplicate emails if similar ones already exist.
```

---

### Test 6: Workflow Health Readback Should Catch Dead Ends

Prompt:

```text
Check the workflow health before I publish it. Do not publish yet.
```

Expected:

```text
- It should call get_workflow and use the returned health object.
- If the outcome emails have no outgoing paths, it may report dead-end warnings.
- It should not publish.
- It should ask what should happen after the dead-end steps or suggest adding End steps.
```

Unexpected:

```text
- It says the workflow is complete without reading back workflow health.
- It publishes without confirmation.
- It ignores disconnected outcomes or dead-end steps.
```

Follow-up prompt:

```text
Add End steps after each of the three applicant outcome emails.
```

Expected:

```text
- It should add End steps after Interview invitation, Application update, and More information needed.
- It should avoid overlapping layout where possible.
- A final get_workflow health readback should show no blocking issues.
```

Unexpected:

```text
- It creates overlapping or duplicate End steps unnecessarily.
- It leaves one branch without an End step.
```

---

### Test 7: Publish Workflow

Prompt:

```text
Publish this workflow.
```

Expected:

```text
- It should call publish_workflow once; no confirm flag is needed.
- It should show health_warnings even if empty.
- It should save a revision before publishing.
- It should return published=true.
```

Unexpected:

```text
- It hides warnings.
- It asks for a redundant confirmation instead of publishing.
- It publishes a different workflow.
```

---

### Test 8: Revision List and Restore Preview

Prompt:

```text
List the latest revisions for this workflow. Then preview restoring the latest revision, but do not restore it yet.
```

Expected:

```text
- It should call list_workflow_revisions.
- It should call restore_workflow_revision with confirm=false.
- It should show revision_id, timestamp, reason, target_step_count, and target_link_count.
- It should not restore.
```

Unexpected:

```text
- It restores without confirmation.
- It cannot list revisions after mutating operations.
```

Follow-up prompt:

```text
Do not restore. Keep the current workflow.
```

Expected:

```text
- No restore call with confirm=true.
```

---

### Test 9: Duplicate Guardrail

Prompt:

```text
Add another email step named "Application received" with the same recipient, subject, and body as the existing Application received email.
```

Expected:

```text
- It should detect a similar existing step.
- It should return existing_step_id and not create a duplicate.
- It should suggest using build_workflow_bulk to reuse/update/wire it, or ask for allow_duplicate only if I really want another one.
```

Unexpected:

```text
- It silently creates a duplicate email step.
```

Follow-up prompt:

```text
Do not duplicate it. Keep the existing step.
```

Expected:

```text
- No new step should be created.
```

---

### Test 10: Delete Step Preview and Verified Delete

Prompt:

```text
Preview deleting the "More information needed" email step. Do not delete it yet.
```

Expected:

```text
- It should call delete_step with confirm=false.
- It should show affected connections.
- It should not delete anything.
```

Unexpected:

```text
- It disconnects instead of previewing delete.
- It deletes immediately.
```

Follow-up prompt:

```text
Confirmed. Delete the "More information needed" email step.
```

Expected:

```text
- It should call delete_step with confirm=true.
- It should delete incident links.
- It should clear any related outcome linkID references.
- It should return deleted=true and verified=true.
```

Unexpected:

```text
- The step is deleted but dangling links remain.
- The HR task outcome still points to a deleted linkID.
```

---

### Test 11: Restore After Delete

Prompt:

```text
I changed my mind. Preview restoring the latest revision so the deleted email step comes back. Do not restore until I confirm.
```

Expected:

```text
- It should call restore_workflow_revision with confirm=false.
- It should show which revision will be restored.
```

Unexpected:

```text
- It restores immediately.
```

Follow-up prompt:

```text
Confirmed. Restore that revision.
```

Expected:

```text
- It should call restore_workflow_revision with confirm=true.
- It should first back up the current state.
- The deleted step and its links should come back if the revision contains them.
```

Unexpected:

```text
- It restores but loses unrelated steps.
- It does not back up the current state before restore.
```

---

### Test 12: Condition Field Guardrail

Prompt:

```text
Add a conditional branch after the Application received email. Branch candidates by the field "Years of Experience": Senior if greater than 5, Junior otherwise.
```

Expected:

```text
- It should use trigger_form_fields from get_workflow / build_workflow_bulk if it needs field ids.
- It should use the real field_id for Years of Experience, not the label text.
- If it cannot safely build the condition, it should ask which real field to use.
```

Unexpected:

```text
- It sends "Years of Experience" as the condition field value.
- It creates conditionTerms with empty or invalid field references.
```

Follow-up prompt if it asks for field confirmation:

```text
Use the real "Years of Experience" field from the trigger form. Create two branches: Senior Candidate for greater than 5, and Junior Candidate for less than or equal to 5.
```

Expected:

```text
- It should use the actual field id from trigger_form_fields.
```

---

### Test 13: Wait for Duration Guardrail

Prompt:

```text
Add a Wait for Duration step for 2 days after the HR review outcome.
```

Expected:

```text
- It should not guess which HR outcome to use.
- It should ask which outcome should lead into the wait step.
```

Unexpected:

```text
- It creates the wait step connected to a random outcome.
```

Follow-up prompt:

```text
Use the "Request More Information" outcome. After waiting 2 days, connect it to the "More information needed" email.
```

Expected:

```text
- It should add workflow_pause_duration or equivalent schema-safe step.
- It should persist pause.executeWhen.afterAmount = 2 and afterUnit = day.
- It should connect the Request More Information outcome to the wait step, then wait step to the email.
```

Unexpected:

```text
- Wait config is created but read-back shows no duration.
- The outcome email gets disconnected without a valid replacement.
```

---

### Test 14: PDF Step Guardrail

Prompt:

```text
Add a PDF step after the Application received email if supported. Do not create an incomplete PDF step. Ask me for missing PDF details if needed.
```

Expected:

```text
- It should recognize PDF as workflow_send_pdf if supported.
- It should ask for recipient, PDF/document selection, subject/body, and connection location if required.
- It should not create a broken PDF step with pdfattachment=0.
```

Unexpected:

```text
- It creates a disconnected or incomplete PDF step.
```

Follow-up prompt:

```text
Send the PDF to the applicant Email Address field. Use subject "Application PDF copy" and body "Hi {Full Name}, attached is a PDF copy of your application." Use the submission PDF if it can be enabled safely; otherwise stop and tell me what PDF document ID or selection is required.
```

Expected:

```text
- If submission PDF cannot be enabled safely, it should stop and ask for the required PDF document ID/selection.
- It should not claim the PDF is configured if read-back shows pdfattachment=0.
```

---

### Test 15: Team Approval Guardrail

Prompt:

```text
Add a Team Approval step after Application received if supported. Do not create it if a team identifier is required and missing.
```

Expected:

```text
- It should identify Team Approval support.
- It should ask for the Jotform team identifier if required.
- It should not create an incomplete Team Approval step.
```

Unexpected:

```text
- It creates a Team Approval step without a team id.
```

Follow-up prompt:

```text
Use team ID TEAM_ID_PLACEHOLDER. Name the step "Team candidate approval". Description: "Team reviews the candidate before HR proceeds." Outcomes: Approved, Rejected. Connect it after Application received.
```

Expected:

```text
- It should create the step only if the team ID and required fields fit the schema.
```

---

### Test 16: Approve & Sign Guardrail

Prompt:

```text
Add an Approve & Sign step assigned to legal@example.com if supported. Do not create an empty or ambiguous step.
```

Expected:

```text
- It should identify Approve & Sign support.
- It should ask for step name, description, outcomes, and where to connect it if missing.
```

Unexpected:

```text
- It creates a vague approval step with no description/outcomes.
```

Follow-up prompt:

```text
Name it "Legal approval and signature". Description: "Legal reviews and signs off before the candidate offer process continues." Outcomes: Approved, Rejected. Connect it after the "Proceed to Interview" branch email if that does not break existing routing; otherwise preview what needs to change first.
```

Expected:

```text
- It should not break existing routing silently.
- It should preview or ask before rewiring occupied paths.
```

---

### Test 17: Verify Payment Guardrail

Prompt:

```text
Add a Verify Payment step if supported. Do not create it unless all required payment information is available.
```

Expected:

```text
- It should ask for payment form ID and verifier information if manual verification is needed.
- It should use schema-safe outcomes only, such as Verify and Not Verify.
```

Unexpected:

```text
- It creates Verify Payment without payment form ID.
- It invents unsupported custom outcomes.
```

Follow-up prompt:

```text
Use payment form ID PAYMENT_FORM_ID_PLACEHOLDER. Use manual verification. Verifier: finance@example.com. Step name: "Verify application payment". Description: "Finance verifies whether the candidate payment was completed." Use outcomes Verify and Not Verify.
```

Expected:

```text
- It should create the payment verification step only if these fields match the schema.
- Read-back should show formID, verificationMethod=manual, approver/verifier info, and Verify/Not Verify outcomes.
```

---

## Part 2: One Large Prompt for a Fresh Session

Paste this into a new ChatGPT session with the Jotform Workflow MCP plugin
enabled.

```text
I want to run a full feature test of the Jotform Workflow MCP plugin. Please build a candidate screening workflow, but follow these rules strictly:

1. Do not create a workflow without a trigger form.
2. If a workflow needs a form, first decide whether to use an existing form or create a new AI form. For this test, create a new AI form.
3. Default language must be English.
4. Use real form field references for recipients and email content. Do not use raw field labels as if they were field IDs.
5. Before adding any task, approval, assignment, condition, PDF, payment, or outcome-based step, ask for missing essential details instead of creating an empty placeholder.
6. Before publishing, inspect the workflow health and report any warnings, then publish with a single publish_workflow call.
7. Before deleting or restoring, use the preview/confirmation flow.
8. Use intent and reason fields for mutating tool calls with short privacy-conscious summaries.
9. Always show direct Jotform links for workflow and form results.

Create a new workflow named "Candidate Screening Workflow - MCP Full Test".

Create a new AI trigger form that collects:
- Full Name
- Email Address
- Phone Number
- Position Applied For
- Years of Experience
- Portfolio URL
- Expected Salary

Make the key contact and application fields required.

After the On Submission trigger, add an email step:
- Name: Application received
- Recipient: applicant's real Email Address field
- Subject: We received your application
- Body: Hi {Full Name}, thank you for applying for {Position Applied For}. We received your application and will review it soon.

After that email, add an HR review task:
- Name: HR review
- Assignee: hr@example.com
- Task description: Review the candidate application, years of experience, portfolio URL, and expected salary. Choose the next action.
- Outcomes:
  - Proceed to Interview
  - Reject
  - Request More Information

Create and connect these outcome emails:

For "Proceed to Interview":
- Email name: Interview invitation
- Recipient: applicant's real Email Address field
- Subject: Interview invitation
- Body: Hi {Full Name}, we would like to invite you to an interview for {Position Applied For}.

For "Reject":
- Email name: Application update
- Recipient: applicant's real Email Address field
- Subject: Application update
- Body: Hi {Full Name}, thank you for your interest. We will not move forward at this time.

For "Request More Information":
- First add a Wait for Duration step for 2 days.
- Then connect it to an email named More information needed.
- Recipient: applicant's real Email Address field
- Subject: More information needed
- Body: Hi {Full Name}, we need a little more information before continuing your application.

Add End steps after the final email in each branch.

After building, run get_workflow. Report:
- Workflow builder link
- Trigger form builder link
- All steps and connections
- Each HR outcome and which step it connects to
- Whether there are unreachable steps, dead ends, dangling links, unconnected branches, invalid branch mappings, or unlabelled branching links
- Any warnings from normalization

Then publish the workflow and report any publish warnings.

After publishing, list the latest workflow revisions and preview restoring the latest revision without restoring it. Do not restore unless I explicitly confirm.
```

Expected from the one large prompt:

```text
- It should create an AI form and workflow together.
- It should show direct workflow/form builder links.
- It should use real form fields for email recipients and body tokens.
- It should create the HR task with three outcomes.
- It should connect outcome branches correctly.
- It should create a 2-day wait step only for Request More Information.
- It should add End steps.
- get_workflow health should ideally show no issues after End steps.
- publish_workflow should be called once without a confirm flag.
- restore_workflow_revision should be previewed with confirm=false only.
```

Unexpected from the one large prompt:

```text
- Workflow is created without a trigger form.
- Email recipients are plain text instead of form field references.
- Task outcomes exist but are not selectable/connected.
- Any outcome email remains disconnected without being reported.
- It asks for a redundant publish confirmation.
- It restores without explicit confirmation.
- It creates duplicate steps unnecessarily.
```
