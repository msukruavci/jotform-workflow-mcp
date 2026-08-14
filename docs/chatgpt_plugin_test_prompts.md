# ChatGPT Plugin Test Prompt Set

Paste these prompts into ChatGPT one by one. This sequence tests the MCP server end to end:

- asks for the trigger form strategy before creating a workflow
- creates a new AI form and binds it as the workflow trigger
- shows direct workflow/form URLs
- asks for missing task, approval, email, condition, and outcome details
- uses real form field IDs instead of plain text labels for condition fields
- checks workflow gaps before presenting the workflow as ready
- creates revision logs and restores a previous revision
- keeps audit logs per server session

## Test 1 - Workflow Trigger Strategy

```text
I want to create a new workflow for a candidate application process.
```

Expected behavior:

- It should not create the workflow immediately.
- It should briefly ask whether to use an existing form or create a new form.

## Test 2 - Create With A New AI Form

```text
Create a new form. Keep it in English. The form should collect candidate applications with full name, email, date of birth, position, years of experience, and a short motivation statement. The workflow name should be Candidate Application Review.
```

Expected behavior:

- It should use `create_workflow_with_ai_form`.
- The response should include direct links:
  - `https://www.jotform.com/workflow/{workflow_id}/build`
  - `https://www.jotform.com/build/{form_id}`

## Test 3 - Missing Task Detail Guardrail

```text
Add a task step to the workflow.
```

Expected behavior:

- It should not create an empty task.
- It should ask a short question: who should the task be assigned to, and what should they do?

## Test 4 - Provide Task Details

```text
Assign the task to hiring.manager@example.com. They should review the candidate application and decide whether the candidate is suitable.
```

Expected behavior:

- It should create a `workflow_assign_task` step with `add_step`.
- If needed, it should read back the workflow and report the step ID and connection state.
- A revision should be saved automatically before the write.

## Test 5 - Missing Approval Detail Guardrail

```text
Add an approval step after the task.
```

Expected behavior:

- It should not create an empty approval.
- It should ask who the approver is and what they are approving.

## Test 6 - Provide Approval Details

```text
The approver should be hr.lead@example.com. The approval is about whether the candidate should move to the next stage.
```

Expected behavior:

- It should create the approval step.
- It should not leave approval outcomes empty.
- It should connect the step correctly or ask a clear wiring question if needed.

## Test 7 - Plain Text Condition Field Trap

```text
If the candidate date of birth starts with 2006-08-07, send it to the Rejected Application branch. Use Date of birth as the field.
```

Expected behavior:

- It should not write `field: "Date of birth"` as plain text.
- It should call `get_form_fields` or `inspect_workflow_gaps`.
- It should resolve the real field ID from the trigger form or ask which field to use.

## Test 8 - Conditional Branch With A Real Field

```text
Use the date of birth field. Create two branches: Accepted Application and Rejected Application. Rejected Application should use a startsWith condition for 2006-08-07. Everything else should go to Accepted Application.
```

Expected behavior:

- It should use the real form field ID.
- It should not send empty `conditionTerms` for a CUSTOM branch.
- If it uses an OTHER/catch-all branch, that branch may have empty `conditionTerms`.
- Outcome names should be readable in connections.

## Test 9 - Missing Email Detail Guardrail

```text
After the Accepted Application branch, send an email to the candidate.
```

Expected behavior:

- It should not create an empty email step.
- It should ask who the email goes to, what the subject is, and what the short message should say.
- It should clarify whether the recipient should come from the submitter email field or be a fixed email address.

## Test 10 - Use Submitter Email Field

```text
Send the email to the candidate's email field from the form. Subject: Application received. Message: Thank you for applying. Our team will review your application and contact you soon.
```

Expected behavior:

- It should use the real email field ID from the form.
- It should use a form field reference instead of a plain text recipient when appropriate.
- It should create the email step and connect it to the accepted branch.

## Test 11 - Email For The Rejected Branch

```text
After the Rejected Application branch, send an email to the candidate too. Use the same form email field. Subject: Application update. Message: Thank you for your interest. Unfortunately, we cannot proceed with this application.
```

Expected behavior:

- It should create a separate email step for the rejected branch.
- It should connect it to the correct outcome.

## Test 12 - Gap Inspection

```text
Is this workflow ready? Check for missing links, empty assignees, empty conditions, unconnected outcomes, or invalid fields.
```

Expected behavior:

- It should call `inspect_workflow_gaps`.
- If there are issues, it should list them clearly.
- If details are missing, it should ask one short question about how to complete them.
- If there are no blocking issues, it should say the workflow is ready and include the workflow URL.

## Test 13 - Get Workflow Readback

```text
Show me the current workflow step by step. Include which outcome goes where, and include the direct workflow link.
```

Expected behavior:

- It should call `get_workflow`.
- It should show connections with outcome labels.
- It should include the workflow URL.

## Test 14 - List Revisions

```text
Show me the revision history for this workflow. The latest 5 revisions are enough.
```

Expected behavior:

- It should call `list_workflow_revisions`.
- It should show revision ID, timestamp, reason, step count, and link count.

## Test 15 - Make A Small Change To Create A Revision

```text
Change the task description to: Review the candidate profile and decide whether HR should continue with the interview process.
```

Expected behavior:

- A revision should be saved automatically before `update_step`.
- It should read back the result afterward.

## Test 16 - Preview Restore To Previous Revision

```text
I want to go back to the previous revision. Show me what it would restore first.
```

Expected behavior:

- It should call `restore_workflow_revision` with `confirm=false`.
- It should show which revision would be restored and how many steps/links it contains.
- It should ask for explicit confirmation.

## Test 17 - Confirm Revision Restore

```text
I confirm. Restore the previous revision.
```

Expected behavior:

- It should call `restore_workflow_revision` with `confirm=true`.
- It should save the current workflow as a backup revision before restoring.
- It should verify afterward with `get_workflow` or `inspect_workflow_gaps`.

## Test 18 - Publish Safety Preview

```text
Publish the workflow.
```

Expected behavior:

- It should not publish immediately.
- It should first use `inspect_workflow_gaps` and/or `publish_workflow(confirm=false)` to show a preview.
- It should ask for explicit confirmation.

## Test 19 - Confirm Publish

```text
I confirm. Publish it.
```

Expected behavior:

- It should call `publish_workflow(confirm=true)`.
- It should include the workflow URL in the response.

## Test 20 - Audit Log Check

```text
Did you log the tool calls and Jotform requests from this session? Tell me where the log file is.
```

Expected behavior:

- It should mention the session-based log path: `mcp_server/logs/sessions/{timestamp}_{session_id}.jsonl`.
- It should summarize the log without exposing secrets such as API keys.
