"""Tests for the plugin entry point — register(ctx).

Loads the repo-root ``__init__.py`` exactly the way hermes'
``PluginManager._load_directory_module`` does (spec_from_file_location
with submodule_search_locations), then drives ``register(ctx)`` with a
recording fake of PluginContext.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

from hermes_mobile.auth_provider import MobileDeviceProvider
from hermes_mobile.device_store import DeviceStore

REPO_ROOT = Path(__file__).resolve().parent.parent


class FakeCtx:
    """Records PluginContext registration calls."""

    def __init__(self) -> None:
        self.auth_providers: list = []
        self.cli_commands: list = []
        self.platforms: list = []

    def register_dashboard_auth_provider(self, provider) -> None:
        self.auth_providers.append(provider)

    def register_platform(self, **kwargs) -> None:
        self.platforms.append(kwargs)

    def register_cli_command(
        self, name, help, setup_fn, handler_fn=None, description=""
    ):
        self.cli_commands.append({
            "name": name,
            "help": help,
            "setup_fn": setup_fn,
            "handler_fn": handler_fn,
            "description": description,
        })


@pytest.fixture
def plugin_module() -> types.ModuleType:
    """Import repo-root __init__.py like hermes' _load_directory_module."""
    module_name = "hermes_plugins.hermes_mobile_test"
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []  # type: ignore[attr-defined]
        sys.modules["hermes_plugins"] = ns
    spec = importlib.util.spec_from_file_location(
        module_name,
        REPO_ROOT / "__init__.py",
        submodule_search_locations=[str(REPO_ROOT)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    module.__package__ = module_name
    module.__path__ = [str(REPO_ROOT)]  # type: ignore[attr-defined]
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(module_name, None)


def test_register_exists(plugin_module):
    assert callable(getattr(plugin_module, "register", None))


def test_register_registers_mobile_device_provider(plugin_module, tmp_path):
    ctx = FakeCtx()
    store = DeviceStore(path=tmp_path / "devices.json")
    plugin_module.register(ctx, _store=store)
    assert len(ctx.auth_providers) == 1
    provider = ctx.auth_providers[0]
    # NB: not isinstance() — the loader imports the package under the
    # hermes_plugins.* namespace, so its MobileDeviceProvider is a
    # distinct (but identical) class object from our direct import.
    assert type(provider).__name__ == MobileDeviceProvider.__name__
    assert provider.name == "mobile-device"
    assert provider.display_name == MobileDeviceProvider.display_name


def test_register_with_real_signature(plugin_module):
    # The hermes loader calls register(ctx) with a single positional arg.
    ctx = FakeCtx()
    plugin_module.register(ctx)
    assert len(ctx.auth_providers) == 1


def test_register_registers_cli_command(plugin_module, tmp_path):
    ctx = FakeCtx()
    plugin_module.register(ctx, _store=DeviceStore(path=tmp_path / "d.json"))
    assert [c["name"] for c in ctx.cli_commands] == ["mobile"]


def test_register_registers_mobile_platform(plugin_module, tmp_path):
    ctx = FakeCtx()
    plugin_module.register(ctx, _store=DeviceStore(path=tmp_path / "d.json"))
    assert len(ctx.platforms) == 1
    entry = ctx.platforms[0]
    assert entry["name"] == "mobile"
    assert entry["label"] == "Mobile"
    assert callable(entry["adapter_factory"])
    assert entry["check_fn"]() is True


def test_register_skips_cli_until_module_exists(plugin_module):
    # hermes_mobile.cli is a later milestone; until it exists register()
    # must not fail and must not register phantom commands.
    ctx = FakeCtx()
    plugin_module.register(ctx)
    try:
        import hermes_mobile.cli  # noqa: F401

        cli_exists = True
    except ImportError:
        cli_exists = False
    if not cli_exists:
        assert ctx.cli_commands == []


def test_plugin_yaml_manifest():
    yaml = pytest.importorskip("yaml")
    manifest = yaml.safe_load((REPO_ROOT / "plugin.yaml").read_text())
    assert manifest["name"] == "hermes-mobile"
    assert manifest["kind"] in {
        "standalone",
        "backend",
        "exclusive",
        "platform",
        "model-provider",
    }
    assert manifest.get("requires_env", []) == []
