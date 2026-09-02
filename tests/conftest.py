from __future__ import annotations

import importlib.util
import os
import queue
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _hermes_is_importable() -> bool:
    try:
        return importlib.util.find_spec("agent.subagent_lifecycle") is not None
    except (ImportError, ModuleNotFoundError):
        return False


def _ensure_hermes_importable() -> None:
    if _hermes_is_importable():
        return

    candidates: list[Path] = []
    if configured := os.environ.get("HERMES_AGENT_ROOT"):
        candidates.append(Path(configured).expanduser())
    if local_app_data := os.environ.get("LOCALAPPDATA"):
        candidates.append(Path(local_app_data) / "hermes" / "hermes-agent")

    for candidate in candidates:
        if (candidate / "agent" / "subagent_lifecycle.py").is_file() and (
            candidate / "tools" / "delegate_tool.py"
        ).is_file():
            sys.path.insert(0, str(candidate))
            if _hermes_is_importable():
                return

    raise pytest.UsageError(
        "Hermes Agent is required to run this suite. Install Hermes, expose its "
        "source through PYTHONPATH, or set HERMES_AGENT_ROOT to the hermes-agent "
        "directory."
    )


_ensure_hermes_importable()

from support import FakeContext
from tools.async_delegation import _reset_for_tests
from tools.process_registry import process_registry


def _prime_pytest_root_package() -> None:
    """Let pytest set up a hyphenated plugin root without a bare relative import."""
    if "__init__" in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        "__init__", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["__init__"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)


_prime_pytest_root_package()


@pytest.fixture
def plugin_package():
    name = f"delegate_task_advanced_test_plugin_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    try:
        yield module
    finally:
        for module_name in tuple(sys.modules):
            if module_name == name or module_name.startswith(f"{name}."):
                sys.modules.pop(module_name, None)


@pytest.fixture
def plugin_module(plugin_package):
    return sys.modules[f"{plugin_package.__name__}.plugin"]


@pytest.fixture
def fake_context() -> FakeContext:
    return FakeContext()


@pytest.fixture
def registered_plugin(plugin_package, plugin_module, fake_context):
    plugin_package.register(fake_context)
    return SimpleNamespace(
        package=plugin_package,
        module=plugin_module,
        context=fake_context,
        tool=fake_context.tools[0],
        handler=fake_context.tools[0]["handler"],
    )


@pytest.fixture
def stub_delegation(monkeypatch):
    """Stub the external dispatch boundary while exercising the public handler."""

    def install(module, *, response=None, before_return=None, exception=None):
        calls = []
        result = response or {
            "status": "dispatched",
            "delegation_id": "deleg-test",
        }

        def fake_dispatch(**kwargs):
            calls.append(kwargs)
            if before_return is not None:
                before_return(kwargs)
            if exception is not None:
                raise exception
            return dict(result)

        # Keep the adapter in one place so implementation refactors only affect
        # this fixture, while each test remains focused on observable behavior.
        if hasattr(module, "dispatch_async_delegation"):
            monkeypatch.setattr(module, "dispatch_async_delegation", fake_dispatch)
        elif hasattr(module, "dispatch_completion_watcher"):
            monkeypatch.setattr(module, "dispatch_completion_watcher", fake_dispatch)
        else:
            raise AssertionError("Plugin exposes no delegation dispatch boundary")

        if hasattr(module, "create_live_transcripts"):
            monkeypatch.setattr(
                module,
                "create_live_transcripts",
                lambda *_args, **_kwargs: ("test-live", [], []),
            )

        return calls

    return install


def _drain_completion_queue() -> None:
    while True:
        try:
            process_registry.completion_queue.get_nowait()
        except queue.Empty:
            return


@pytest.fixture
def clean_runtime_registry():
    _reset_for_tests()
    _drain_completion_queue()
    try:
        yield
    finally:
        _reset_for_tests()
        _drain_completion_queue()
