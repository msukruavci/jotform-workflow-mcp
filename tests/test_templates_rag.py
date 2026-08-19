import pytest
from mcp_server import rag_engine
from mcp_server.tools.templates import (
    search_templates_tool,
    get_template_detail_tool,
    WorkflowTemplateDetail,
)


def test_rag_engine_search():
    results = rag_engine.search_templates("recruiting candidate hiring", top_k=2)
    assert isinstance(results, list)
    assert len(results) <= 2
    if results:
        assert "id" in results[0]
        assert "title" in results[0]
        assert "score" in results[0]


def test_search_workflow_templates_tool():
    res = search_templates_tool(query="vacation approval flow", top_k=3)
    assert res.query == "vacation approval flow"
    assert isinstance(res.count, int)
    assert isinstance(res.templates, list)
    if res.templates:
        first = res.templates[0]
        assert first.id
        assert first.title
        assert isinstance(first.steps_summary, list)


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
