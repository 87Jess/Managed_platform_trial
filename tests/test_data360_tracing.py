from unittest.mock import MagicMock, call

import pytest
from opentelemetry import trace

from data360agent.agent import Data360Agent, VertexData360Agent, root_agent


@pytest.mark.parametrize("enabled", [True, False])
def test_query_and_tool_spans_are_opt_in_and_do_not_capture_inputs(monkeypatch, enabled):
    tracer = MagicMock()
    monkeypatch.setattr(trace, "get_tracer", lambda name: tracer)
    client = MagicMock()
    client.__enter__.return_value.call_tool.return_value = {"value": "result"}
    agent = Data360Agent(client_factory=lambda: client, enable_tracing=enabled)

    result = agent.query(
        '{"action":"call_tool","name":"data360_get_data","arguments":{"country":"IND"}}'
    )

    assert result["result"] == {"value": "result"}
    expected = [
        call(name, record_exception=False, set_status_on_exception=False)
        for name in ("data360.query", "data360.call_tool")
    ] if enabled else []
    assert tracer.start_as_current_span.call_args_list == expected


def test_tracing_marks_errors_and_preserves_the_exception(monkeypatch):
    tracer = MagicMock()
    monkeypatch.setattr(trace, "get_tracer", lambda name: tracer)
    assert root_agent.enable_tracing is True
    with pytest.raises(ValueError, match="Malformed JSON command"):
        root_agent.query("{invalid")
    span = tracer.start_as_current_span.return_value.__enter__.return_value
    span.set_status.assert_called_once_with(trace.StatusCode.ERROR)
    span.record_exception.assert_not_called()


def test_sdk_wrapper_runs_existing_workflow_with_tracing(monkeypatch):
    vertex_sdk = pytest.importorskip("vertexai.agent_engines")
    from google.cloud.aiplatform import initializer
    from google.oauth2.credentials import Credentials

    wrapper_settings = []
    sdk_cls = vertex_sdk.LangchainAgent

    def create_wrapper(**kwargs):
        wrapper = sdk_cls(**kwargs)
        wrapper_settings.append((kwargs["enable_tracing"], initializer.global_config.project))
        return wrapper

    tracer = MagicMock()
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setattr(initializer.global_config, "_project", None)
    monkeypatch.setattr(initializer.global_config, "_credentials", Credentials("test-token"))
    lookup = MagicMock(side_effect=RuntimeError("Cloud Resource Manager is disabled"))
    monkeypatch.setattr(initializer.resource_manager_utils, "get_project_id", lookup)
    monkeypatch.setattr(trace, "get_tracer", lambda name: tracer)
    monkeypatch.setattr(vertex_sdk, "LangchainAgent", create_wrapper)
    workflow = MagicMock(return_value={"reply": "Data360 response", "tools_used": []})
    monkeypatch.setattr(Data360Agent, "_converse", workflow)
    agent = VertexData360Agent(enable_tracing=True)
    history = [{"role": "user", "content": "Ghana unemployment"}]

    assert agent.query(message="Since 2010?", history=history)["reply"] == "Data360 response"
    assert agent.query(input="Start over")["reply"] == "Data360 response"
    assert wrapper_settings == [(True, "test-project")]
    lookup.assert_not_called()
    assert workflow.call_args_list == [call("Since 2010?", history), call("Start over", [])]
    assert agent._tracer is tracer
