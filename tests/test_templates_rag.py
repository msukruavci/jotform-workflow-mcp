import pytest
from mcp_server import rag_engine
from mcp_server.tools.templates import (
    search_templates_tool,
    get_template_detail_tool,
    WorkflowTemplateDetail,
    _sanitize_template_element,
)


def test_rag_engine_search():
    results = rag_engine.search_templates("recruiting candidate hiring", top_k=2)
    assert isinstance(results, list)
    assert len(results) <= 2
    if results:
        assert "id" in results[0]
        assert "title" in results[0]
        assert "score" in results[0]


def test_vector_search_failure_uses_relevant_lexical_fallback(monkeypatch):
    class BrokenIndex:
        def search(self, *args, **kwargs):
            raise RuntimeError("index unavailable")

    class Model:
        def embed(self, values):
            return [[1.0, 0.0] for _ in values]

    dataset = [
        {"id": "unrelated", "title": "Wedding RSVP", "search_text": "wedding guest"},
        {"id": "expense", "title": "Expense Approval", "search_text": "expense approval reimbursement"},
    ]
    monkeypatch.setattr(rag_engine, "load_dataset", lambda: dataset)
    monkeypatch.setattr(rag_engine, "get_faiss_index", lambda: BrokenIndex())
    monkeypatch.setattr(rag_engine, "_get_embedding_model", lambda: Model())

    results = rag_engine.search_templates("expense approval", top_k=1)

    assert [item["id"] for item in results] == ["expense"]


def test_search_workflow_templates_tool():
    res = search_templates_tool(query="vacation approval flow")
    assert res.query == "vacation approval flow"
    assert isinstance(res.count, int)
    assert res.count <= 1
    assert isinstance(res.templates, list)
    if res.templates:
        first = res.templates[0]
        assert first.id
        assert first.title
        assert isinstance(first.steps_summary, list)
        assert isinstance(first.elements, list)
        assert isinstance(first.links, list)
        assert first.elements_count == len(first.elements)
        assert first.links_count == len(first.links)


def test_template_blueprint_keeps_useful_fields_without_html_noise():
    email = _sanitize_template_element({
        "element_id": "7",
        "type": "workflow_send_email",
        "name": "Notify Applicant",
        "to": [{"id": "uuid-noise", "value": "{email3}", "text": "Applicant Email"}],
        "subject": "Application received",
        "content": "<table><tr><td>Huge branded body</td></tr></table>",
        "x": 100,
        "y": 200,
    })
    approval = _sanitize_template_element({
        "element_id": "8",
        "type": "workflow_approval",
        "name": "HR Approval",
        "approver": [{"id": "uuid-noise", "value": "hr@draft.internal", "text": "HR"}],
        "approvalEmail__email__subject": "Your action required.",
        "approvalEmail__email__content": "<table>large body</table>",
        "taskDescription": "Review the application.",
    })

    assert email == {
        "element_id": "7",
        "type": "workflow_send_email",
        "name": "Notify Applicant",
        "to": ["Applicant Email"],
        "subject": "Application received",
    }
    assert approval == {
        "element_id": "8",
        "type": "workflow_approval",
        "name": "HR Approval",
        "approver": ["HR"],
        "subject": "Your action required.",
        "taskDescription": "Review the application.",
    }


def test_search_workflow_templates_caps_top_k_at_three():
    res = search_templates_tool(query="approval flow", top_k=10)

    assert res.count <= 3


def test_turkish_queries_are_expanded_and_reranked_in_english_space():
    leave = search_templates_tool(query="izin talebi onay akışı", top_k=1)
    internship = search_templates_tool(query="staj başvuru süreci", top_k=1)

    assert "leave" in leave.normalized_query
    assert "Day Off" in leave.templates[0].title
    assert "internship" in internship.normalized_query
    assert "Recruiting" in internship.templates[0].title
    assert leave.templates[0].suggested_form_fields


def test_get_workflow_template_tool():
    # Test retrieving a known template ID
    dataset = rag_engine.load_dataset()
    assert len(dataset) > 0
    sample_id = dataset[0]["id"]
    
    detail = get_template_detail_tool(sample_id)
    assert isinstance(detail, WorkflowTemplateDetail)
    assert detail.id == sample_id
    assert detail.title
    assert isinstance(detail.elements, list)
    assert isinstance(detail.links, list)
    assert detail.elements_count == len(detail.elements)
    assert detail.links_count == len(detail.links)


def test_get_workflow_template_not_found():
    with pytest.raises(ValueError, match="was not found in catalog"):
        get_template_detail_tool("non_existent_id_99999999")
