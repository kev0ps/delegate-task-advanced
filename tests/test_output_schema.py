import json

from agent.subagent_lifecycle import SubagentLaunchRequest


def test_output_schema_is_validated_and_appended_to_child_context(
    registered_plugin, monkeypatch
):
    schema = {
        "type": "object",
        "properties": {"finding": {"type": "string"}},
        "required": ["finding"],
    }
    monkeypatch.setattr(
        registered_plugin.module,
        "dispatch_completion_watcher",
        lambda **kwargs: {"status": "dispatched", "delegation_id": "deleg-schema"},
    )

    payload = json.loads(
        registered_plugin.handler(
            {
                "name": "Structured",
                "goal": "Review",
                "output_schema": schema,
            }
        )
    )

    assert payload["success"] is True
    request = registered_plugin.context.subagent_lifecycle.requests[0]
    assert "OUTPUT CONTRACT (machine-validated)" in request.context
    result = registered_plugin.module.result_payload(
        registered_plugin.context.subagent_lifecycle,
        registered_plugin.context.subagent_lifecycle.launch(request),
        "Structured",
        schema,
    )
    assert result["schema_valid"] is False
    assert result["schema_errors"]


def test_invalid_output_schema_fails_before_launch(registered_plugin):
    payload = json.loads(
        registered_plugin.handler(
            {
                "name": "Structured",
                "goal": "Review",
                "output_schema": {"type": "definitely-not-a-json-schema-type"},
            }
        )
    )

    assert payload["success"] is False
    assert "output_schema" in payload["error"]
    assert registered_plugin.context.subagent_lifecycle.requests == []


def test_invalid_structured_result_gets_one_bounded_correction_retry(
    plugin_module, fake_lifecycle
):
    schema = {
        "type": "object",
        "properties": {"finding": {"type": "string"}},
        "required": ["finding"],
    }
    request = SubagentLaunchRequest(
        goal="Return structured finding",
        context="OUTPUT CONTRACT (machine-validated)",
        correlation_id="initial",
        metadata={},
    )
    state = plugin_module.DeferredLaunch(fake_lifecycle, "Structured", schema)
    state.retry_request = request
    state.set_handle(fake_lifecycle.launch(request))

    result = state.run()

    assert len(fake_lifecycle.requests) == 2
    assert result["schema_retries"] == 1
    assert result["schema_valid"] is False
    assert result["schema_errors"]
    assert "Correct the previous final response" in fake_lifecycle.requests[1].goal


def test_failed_correction_launch_preserves_first_validation_failure(
    plugin_module, fake_lifecycle, monkeypatch
):
    request = SubagentLaunchRequest(
        goal="Return structured finding",
        context="OUTPUT CONTRACT (machine-validated)",
        correlation_id="initial",
        metadata={},
    )
    initial_handle = fake_lifecycle.launch(request)

    def raise_retry_error(request):
        raise RuntimeError("retry launch failed")

    monkeypatch.setattr(fake_lifecycle, "launch", raise_retry_error)
    state = plugin_module.DeferredLaunch(
        fake_lifecycle,
        "Structured",
        {"type": "object", "required": ["finding"]},
    )
    state.retry_request = request
    state.set_handle(initial_handle)

    result = state.run()

    assert result["status"] == "error"
    assert result["schema_valid"] is False
    assert result["schema_errors"]
    assert result["schema_retries"] == 1
    assert "retry launch failed" in result["schema_retry_error"]
    assert result["exit_reason"] == "output_schema_invalid"
    assert "output_schema validation failed" in result["error"]
