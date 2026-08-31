"""
Automated edge-case test suite for prompt-driven workflows.

Tests:
1. One-shot workflow creation with AI Form in Turkish.
2. Bulk step deletion and replacement in a single atomic updateTree transaction.
3. Deleting branching elements and verifying incident links and outcome cleanup.
4. Sanitized template search and 1-shot blueprint construction.
5. Revisions capture, dry-run restore preview, and confirmed rollback.
"""
from __future__ import annotations

import json
import pytest

from mcp_server.models import ConnectionSpec, StepSpec
from mcp_server.server import mcp
from mcp_server.tools import building, reading, risky, templates


class MockLiveClient:
    def __init__(self):
        self.workflows = {}
        self.forms = {}
        self.update_calls = []
        self.next_element_id = 100
        self.next_link_id = 500

    def create_form_with_ai(self, prompt: str, language: str = "en") -> dict:
        form_id = f"form_{len(self.forms) + 1}"
        form_data = {
            "id": form_id,
            "title": f"Generated Form: {prompt[:30]}",
            "url": f"https://www.jotform.com/build/{form_id}",
            "questions": {
                "1": {"qid": "1", "type": "control_head", "text": "Form Header"},
                "2": {"qid": "2", "type": "control_textbox", "text": "Ad Soyad", "name": "adSoyad"},
                "3": {"qid": "3", "type": "control_email", "text": "E-posta", "name": "email"},
                "4": {"qid": "4", "type": "control_dropdown", "text": "İzin Türü", "name": "izinTuru", "options": "Yıllık İzin|Mazeret|Hastalık"},
            }
        }
        self.forms[form_id] = form_data
        return {
            "form_id": form_id,
            "title": form_data["title"],
            "url": form_data["url"],
            "questions": [
                {"field_id": "2", "name": "adSoyad", "label": "Ad Soyad", "type": "control_textbox"},
                {"field_id": "3", "name": "email", "label": "E-posta", "type": "control_email"},
                {"field_id": "4", "name": "izinTuru", "label": "İzin Türü", "type": "control_dropdown", "options": ["Yıllık İzin", "Mazeret", "Hastalık"]},
            ],
            "language": language,
        }

    def create_workflow(self, title: str, form_id: str | None = None) -> dict:
        wf_id = f"wf_{len(self.workflows) + 1}"
        elements = [
            {"element_id": 1, "type": "workflow_start_point", "position": {"x": 0, "y": 0}, "resourceID": form_id or "", "resourceType": "FORM" if form_id else ""},
            {"element_id": 2, "type": "workflow_end_point", "position": {"x": 0, "y": 600}},
        ]
        self.workflows[wf_id] = {
            "id": wf_id,
            "title": title,
            "elements": elements,
            "links": [],
            "status": "ENABLED",
        }
        return {"id": wf_id, "workflow_id": wf_id, "title": title, "trigger_form_id": form_id}

    def get_workflow(self, workflow_id: str) -> dict:
        if workflow_id not in self.workflows:
            return {"error": "Not found"}
        wf = self.workflows[workflow_id]
        return {
            "workflow": {
                "id": wf["id"],
                "title": wf["title"],
                "elements": list(wf["elements"]),
                "links": list(wf["links"]),
                "status": wf["status"],
            }
        }

    def get_workflow_combined(self, workflow_id: str) -> dict:
        wf = self.workflows.get(workflow_id, {})
        return {
            "id": wf.get("id"),
            "title": wf.get("title"),
            "elements": list(wf.get("elements", [])),
            "links": list(wf.get("links", [])),
            "status": wf.get("status"),
        }

    def assert_workflow_revision(
        self,
        workflow_id: str,
        *,
        expected_revision_id: str | None = None,
        base_updated_at: str | None = None,
    ) -> dict:
        return {
            "revision_id": expected_revision_id,
            "updated_at": base_updated_at,
            "snapshot": self.get_workflow_combined(workflow_id),
        }

    def get_elements(self, workflow_id: str) -> list[dict]:
        wf = self.workflows.get(workflow_id, {})
        return list(wf.get("elements", []))

    def get_element(self, workflow_id: str, element_id: int | str) -> dict:
        wf = self.workflows.get(workflow_id, {})
        for el in wf.get("elements", []):
            if str(el.get("element_id")) == str(element_id):
                return dict(el)
        return {}

    def get_links(self, workflow_id: str) -> list[dict]:
        wf = self.workflows.get(workflow_id, {})
        return list(wf.get("links", []))

    def set_trigger_form(self, workflow_id: str, form_id: str) -> dict:
        wf = self.workflows.get(workflow_id)
        if wf:
            for el in wf.get("elements", []):
                if el.get("type") == "workflow_start_point":
                    el["resourceID"] = form_id
                    el["resourceType"] = "FORM"
        return {"status": "success"}

    def get_form_questions(self, form_id: str) -> dict:
        if form_id in self.forms:
            return self.forms[form_id]["questions"]
        return {}

    def update_tree(
        self,
        workflow_id: str,
        elements: list[dict] | None = None,
        links: list[dict] | None = None,
        expected_revision_id: str | None = None,
        base_updated_at: str | None = None,
    ) -> dict:
        elements = elements or []
        links = links or []
        self.update_calls.append({"workflow_id": workflow_id, "elements": elements, "links": links})
        wf = self.workflows.get(workflow_id)
        if not wf:
            return {"error": "Workflow not found"}

        # Apply link deletions
        for ld in links:
            if ld.get("action") == "delete":
                lid = ld.get("linkID") or ld.get("data", {}).get("link_id")
                wf["links"] = [l for l in wf["links"] if str(l.get("link_id")) != str(lid)]

        # Apply element deletions
        for ed in elements:
            if ed.get("action") == "delete":
                eid = ed.get("elementID") or ed.get("data", {}).get("element_id")
                wf["elements"] = [e for e in wf["elements"] if str(e.get("element_id")) != str(eid)]

        # Apply element updates and creates
        for ed in elements:
            action = ed.get("action")
            data = ed.get("data", {})
            eid = str(ed.get("elementID") or data.get("element_id"))
            if action == "create":
                created_el = dict(data)
                created_el["element_id"] = int(eid) if eid.isdigit() else eid
                wf["elements"].append(created_el)
            elif action == "update":
                for existing in wf["elements"]:
                    if str(existing.get("element_id")) == eid:
                        existing.update(data)

        # Apply link creates
        for ld in links:
            if ld.get("action") == "create":
                created_lk = dict(ld.get("data", {}))
                created_lk["link_id"] = len(wf["links"]) + 100
                wf["links"].append(created_lk)

        return {"status": "success", "content": "Tree updated"}


class DummyMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorator(fn):
            def wrapped(*args, **kwargs):
                if fn.__name__ == "build_workflow_bulk":
                    workflow_id = kwargs.get("workflow_id") or (args[0] if args else "")
                    if workflow_id and not kwargs.get("expected_revision_id") and not kwargs.get("base_updated_at"):
                        kwargs["expected_revision_id"] = "test-revision"
                return fn(*args, **kwargs)

            self.tools[fn.__name__] = wrapped
            return fn
        return decorator


@pytest.fixture
def test_setup():
    mcp_dummy = DummyMCP()
    client = MockLiveClient()
    building.register(mcp_dummy, client)
    reading.register(mcp_dummy, client)
    risky.register(mcp_dummy, client)
    templates.register(mcp_dummy)
    return mcp_dummy, client


def test_edge_case_1_oneshot_ai_form_and_workflow_creation(test_setup):
    """
    Edge Case 1: 1-shot AI Form and Workflow creation with Turkish language and field mapping.
    """
    mcp_dummy, client = test_setup
    steps = [
        StepSpec(
            ref="yonetici_onay",
            type="workflow_approval",
            config={
                "approver": "yonetici@sirket.com",
                "taskDescription": "Lütfen izin talebini inceleyin.",
                "outcomes": ["Approve", "Deny"],
            },
        ),
        StepSpec(
            ref="ik_onay_email",
            type="workflow_send_email",
            config={
                "to": "ik@sirket.com",
                "subject": "İzin Talebi Onaylandı",
                "content": "{adSoyad} adlı çalışanın {izinTuru} talebi onaylandı.",
            },
        ),
        StepSpec(
            ref="calisan_red_email",
            type="workflow_send_email",
            config={
                "to": "{email}",
                "subject": "İzin Talebi Reddedildi",
                "content": "Sayın {adSoyad}, izin talebiniz reddedildi.",
            },
        ),
    ]
    connections = [
        ConnectionSpec(from_ref="start", to_ref="yonetici_onay"),
        ConnectionSpec(from_ref="yonetici_onay", to_ref="ik_onay_email", outcome="Approve"),
        ConnectionSpec(from_ref="yonetici_onay", to_ref="calisan_red_email", outcome="Deny"),
        ConnectionSpec(from_ref="ik_onay_email", to_ref="end"),
        ConnectionSpec(from_ref="calisan_red_email", to_ref="end"),
    ]

    result = mcp_dummy.tools["build_workflow_bulk"](
        title="Yıllık İzin Talep Süreci",
        form_prompt="Çalışan adı soyadı, e-posta ve izin türü içeren Türkçe form",
        form_language="tr",
        steps=steps,
        connections=connections,
    )

    assert result.error is None
    assert result.workflow_id is not None
    assert result.trigger_form_id is not None
    assert len(result.created_steps) == 3
    assert result.created_links_count == 5
    # Read back workflow to verify graph health
    wf_read = mcp_dummy.tools["get_workflow"](result.workflow_id)
    assert len(wf_read.steps) == 5
    assert wf_read.health.unreachable_steps == []


def test_edge_case_2_bulk_step_deletion_and_replacement(test_setup):
    """
    Edge Case 2: Atomic deletion of obsolete steps and insertion of replacements.
    Workflow starts with:
      Start(1) -> Approval(4) --[Approve]--> OldMail1(8) -> OldMail2(9) -> End(2)
                              --[Deny]-----> End(2)
    We delete 8 and 9, and add NewTask(10) -> NewMail(11) -> End(2).
    """
    mcp_dummy, client = test_setup
    # Create base form and workflow
    form_res = client.create_form_with_ai("Test form")
    form_id = form_res["form_id"]
    base_res = client.create_workflow("Mevcut Süreç", form_id=form_id)
    wf_id = base_res["workflow_id"]
    client.workflows[wf_id]["elements"] = [
        {"element_id": 1, "type": "workflow_start_point", "resourceID": form_id, "resourceType": "FORM", "position": {"x": 0, "y": 0}},
        {
            "element_id": 4,
            "type": "workflow_approval",
            "approver": "lead@co.com",
            "outcomes": [{"outcomeID": 1, "name": "Approve", "linkID": 201}, {"outcomeID": 2, "name": "Deny", "linkID": 204}],
            "position": {"x": 0, "y": 100},
        },
        {"element_id": 8, "type": "workflow_send_email", "name": "Old Step 8", "position": {"x": -100, "y": 200}},
        {"element_id": 9, "type": "workflow_send_email", "name": "Old Step 9", "position": {"x": -100, "y": 300}},
        {"element_id": 2, "type": "workflow_end_point", "position": {"x": 0, "y": 500}},
    ]
    client.workflows[wf_id]["links"] = [
        {"link_id": 200, "fromElement": 1, "toElement": 4},
        {"link_id": 201, "fromElement": 4, "toElement": 8},
        {"link_id": 202, "fromElement": 8, "toElement": 9},
        {"link_id": 203, "fromElement": 9, "toElement": 2},
        {"link_id": 204, "fromElement": 4, "toElement": 2},
    ]

    # Model executes 1-shot replacement
    steps = [
        StepSpec(
            ref="it_prep_task",
            type="workflow_assign_task",
            config={"assignee": "it@co.com", "taskDescription": "Ekipman hazırla", "outcomes": ["Tamamlandı"]},
        ),
        StepSpec(
            ref="ready_email",
            type="workflow_send_email",
            config={"to": "{email}", "subject": "Hazır", "content": "Ekipmanınız hazır."},
        ),
    ]
    connections = [
        ConnectionSpec(from_ref="4", to_ref="it_prep_task", outcome="Approve"),
        ConnectionSpec(from_ref="it_prep_task", to_ref="ready_email", outcome="Tamamlandı"),
        ConnectionSpec(from_ref="ready_email", to_ref="2"),
    ]

    result = mcp_dummy.tools["build_workflow_bulk"](
        workflow_id=wf_id,
        delete_step_ids=["8", "9"],
        steps=steps,
        connections=connections,
    )

    assert result.error is None
    assert result.deleted_steps == ["8", "9"]
    assert "it_prep_task" in result.created_steps
    assert "ready_email" in result.created_steps

    # Verify atomic update payload
    call = client.update_calls[0]
    element_deletes = [e for e in call["elements"] if e.get("action") == "delete"]
    assert len(element_deletes) == 2
    assert {e["elementID"] for e in element_deletes} == {"8", "9"}

    link_deletes = [l for l in call["links"] if l.get("action") == "delete"]
    # Links 201, 202, 203 touch 8 and 9 so all 3 must be deleted
    assert {l["linkID"] for l in link_deletes} == {201, 202, 203}

    # Verify get_workflow health
    wf_read = mcp_dummy.tools["get_workflow"](wf_id)
    assert wf_read.health.unreachable_steps == []
    assert wf_read.health.dead_end_steps == []


def test_edge_case_3_delete_branching_step_outcome_cleanup(test_setup):
    """
    Edge Case 3: Deleting an entire branching step (approval) and linking directly.
    Start(1) -> Approval(4) --[Approve]--> Email(5) -> End(2)
                            --[Deny]-----> End(2)
    We delete Approval(4), and connect Start(1) directly to Email(5).
    """
    mcp_dummy, client = test_setup
    base_res = client.create_workflow("Onay Kaldırma", form_id="form_1")
    wf_id = base_res["workflow_id"]
    client.workflows[wf_id]["elements"] = [
        {"element_id": 1, "type": "workflow_start_point", "resourceID": "form_1", "resourceType": "FORM", "position": {"x": 0, "y": 0}},
        {
            "element_id": 4,
            "type": "workflow_approval",
            "approver": "lead@co.com",
            "outcomes": [{"outcomeID": 1, "name": "Approve", "linkID": 201}, {"outcomeID": 2, "name": "Deny", "linkID": 202}],
            "position": {"x": 0, "y": 100},
        },
        {"element_id": 5, "type": "workflow_send_email", "name": "Info Mail", "position": {"x": 0, "y": 200}},
        {"element_id": 2, "type": "workflow_end_point", "position": {"x": 0, "y": 300}},
    ]
    client.workflows[wf_id]["links"] = [
        {"link_id": 200, "fromElement": 1, "toElement": 4},
        {"link_id": 201, "fromElement": 4, "toElement": 5},
        {"link_id": 202, "fromElement": 4, "toElement": 2},
        {"link_id": 203, "fromElement": 5, "toElement": 2},
    ]

    connections = [
        ConnectionSpec(from_ref="1", to_ref="5"),
    ]

    result = mcp_dummy.tools["build_workflow_bulk"](
        workflow_id=wf_id,
        delete_step_ids=["4"],
        steps=[],
        connections=connections,
    )

    assert result.error is None
    assert result.deleted_steps == ["4"]

    call = client.update_calls[0]
    # Check link 200, 201, 202 are deleted
    link_deletes = {l["linkID"] for l in call["links"] if l.get("action") == "delete"}
    assert link_deletes == {200, 201, 202}

    # Verify new link is created from 1 to 5
    link_creates = [l for l in call["links"] if l.get("action") == "create"]
    assert any(l["data"]["fromElement"] == "1" and l["data"]["toElement"] == "5" for l in link_creates)


def test_edge_case_4_revision_safety_and_restoration(test_setup):
    """
    Edge Case 4: Rollback and revision safety.
    Verifies automatic snapshot creation on mutation and restore preview.
    """
    mcp_dummy, client = test_setup
    base_res = client.create_workflow("Revizyon Test", form_id="form_1")
    wf_id = base_res["workflow_id"]

    # Initial bulk build
    steps = [
        StepSpec(ref="task_1", type="workflow_assign_task", config={"assignee": "a@b.com", "taskDescription": "T"}),
    ]
    connections = [
        ConnectionSpec(from_ref="start", to_ref="task_1"),
        ConnectionSpec(from_ref="task_1", to_ref="2"),
    ]
    res1 = mcp_dummy.tools["build_workflow_bulk"](workflow_id=wf_id, steps=steps, connections=connections)
    assert res1.error is None

    # Check revision log
    revs = mcp_dummy.tools["list_workflow_revisions"](wf_id)
    assert len(revs.revisions) >= 1
    newest_rev = revs.revisions[0]

    # Dry-run restore preview
    preview = mcp_dummy.tools["restore_workflow_revision"](wf_id, revision_id=newest_rev.revision_id, confirm=False)
    assert preview.needs_confirmation is True
    assert preview.restored is False
    assert "confirm=true" in preview.hint.lower()

    # Confirmed restore
    restored = mcp_dummy.tools["restore_workflow_revision"](wf_id, revision_id=newest_rev.revision_id, confirm=True)
    assert restored.restored is True
    assert restored.error is None
