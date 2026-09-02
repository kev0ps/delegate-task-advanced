import copy
import inspect


def test_register_adds_advanced_tool_without_touching_native_delegation(
    plugin_package, fake_context
):
    from toolsets import TOOLSETS
    from tools.delegate_tool import DELEGATE_TASK_SCHEMA, delegate_task

    original_toolsets = copy.deepcopy(TOOLSETS)
    original_delegate_toolset = dict(TOOLSETS["delegation"])
    original_delegate_schema = copy.deepcopy(DELEGATE_TASK_SCHEMA)
    original_delegate_handler = delegate_task

    plugin_package.register(fake_context)

    assert [tool["name"] for tool in fake_context.tools] == [
        "delegate_task_advanced"
    ]
    assert fake_context.tools[0]["toolset"] == "delegation"
    assert TOOLSETS == original_toolsets
    assert TOOLSETS["delegation"] == original_delegate_toolset
    assert DELEGATE_TASK_SCHEMA == original_delegate_schema
    assert delegate_task is original_delegate_handler
    assert fake_context.unload_callbacks == []


def test_package_init_exports_only_the_public_register_hook(plugin_package):
    source = inspect.getsource(plugin_package)

    assert "def register(" in source
    assert "def make_handler(" not in source
    assert "SubagentLaunchRequest" not in source


def test_schema_omits_provider_tuning_and_batch_fields(registered_plugin):
    parameters = registered_plugin.tool["schema"]["parameters"]

    assert parameters["additionalProperties"] is False
    assert set(parameters["required"]) == {"name", "goal"}
    assert "model" in parameters["properties"]
    assert "provider" not in parameters["properties"]
    assert "reasoning_effort" not in parameters["properties"]
    assert "role" not in parameters["properties"]
    assert "tasks" not in parameters["properties"]
    assert "output_schema" in parameters["properties"]
    assert "Hermes decides the subagent role" in registered_plugin.tool["schema"][
        "description"
    ]


def test_schema_describes_selection_and_security_boundaries(registered_plugin):
    tool = registered_plugin.tool
    description = tool["schema"]["description"]
    properties = tool["schema"]["parameters"]["properties"]

    for phrase in ("one named Hermes subagent", "fresh conversation", "returns immediately"):
        assert phrase in description
    assert "not the generated subagent_id" in properties["name"]["description"]
    assert "cannot see the parent conversation" in properties["goal"]["description"]
    assert "grant no permissions" in properties["skills"]["description"]
    assert "not individual tool names" in properties["toolsets"]["description"]
    assert "do not imply read-only access" in properties["toolsets"]["description"]
    assert "cannot switch providers" in properties["model"]["description"]
    assert "one named Hermes subagent" in tool["description"]
    assert "delegation depth" in tool["description"]


def test_source_does_not_define_custom_file_readonly_toolset(plugin_module):
    assert "file_readonly" not in inspect.getsource(plugin_module)


def test_routing_context_does_not_use_private_async_api(plugin_module):
    assert "_current_origin_session_id" not in inspect.getsource(
        plugin_module.routing_context
    )
