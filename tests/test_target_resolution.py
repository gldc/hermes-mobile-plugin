"""Explicit ``mobile:<device_id>`` targets — parser, validator, standalone sender.

hermes resolves ``hermes send -t mobile:<id>`` and cron ``deliver=mobile:<id>``
through ``resolve_send_target`` (0.21.5 ``tools/send_message_targets.py``; 0.20.4
``tools/send_message_tool.py``). A plugin platform is only resolvable there through
its PlatformEntry ``parse_target_ref_fn``; a 16-hex device id matches none of the
core's built-in heuristics, so without a parser the CLI fails with "Could not
resolve". Once a parser is registered, cron's ``pass_unresolved_references``
becomes strict for this platform, so the parser must accept every id
``DeviceStore.create_device`` can mint.

Requires hermes' gateway package on PYTHONPATH (the repo test command puts a core
checkout there), like tests/test_adapter.py.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
from typing import Any, Callable, Optional

import pytest

import gateway.platform_registry as registry_mod
from gateway.config import PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry

from hermes_mobile import adapter as adapter_mod
from hermes_mobile.adapter import (
    make_standalone_sender,
    make_target_validator,
    parse_target_ref,
    register_platform,
)
from hermes_mobile.device_store import DeviceStore

DEVICE_ID = "dea107ff349bb429"


class RecordingPush:
    def __init__(self):
        self.sent = []

    def send(self, token, title="Hermes", body=None):
        self.sent.append({"token": token, "title": title, "body": body})
        return True


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def mobile_platform_registered():
    """Make Platform('mobile') resolvable (MobileAdapter's constructor needs it)."""
    if not platform_registry.is_registered("mobile"):
        platform_registry.register(
            PlatformEntry(
                name="mobile",
                label="Mobile",
                adapter_factory=lambda cfg: None,
                check_fn=lambda: True,
            )
        )
        yield
        platform_registry.unregister("mobile")
    else:
        yield


@pytest.fixture
def store(tmp_path) -> DeviceStore:
    return DeviceStore(path=tmp_path / "devices.json")


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ref, expected",
    [
        (DEVICE_ID, DEVICE_ID),
        ("DEA107FF349BB429", DEVICE_ID),
        ("  dea107ff349bb429\n", DEVICE_ID),
        ("\tDea107fF349bB429 ", DEVICE_ID),
        # All-digit ids are valid hex too (token_hex can mint one).
        ("1234567890123456", "1234567890123456"),
    ],
)
def test_parser_accepts_device_ids(ref, expected):
    assert parse_target_ref(ref) == (expected, None)


@pytest.mark.parametrize(
    "ref",
    [
        "",
        "   ",
        "my-iphone",  # device names are not unique: never resolved by name
        "dea107ff349bb42",  # 15 chars
        "dea107ff349bb4290",  # 17 chars
        "dea107ff349bb42g",  # non-hex
        "dea107ff349bb429:1",  # no threads on mobile
        "mobile:dea107ff349bb429",
        "0xdea107ff349bb4",
        "dea107ff 349bb429",
        "#dea107ff349bb429",
        "ｄea107ff349bb429",  # fullwidth letter: not ASCII hex
        "١٢٣٤٥٦٧٨٩٠١٢٣٤٥٦",  # Arabic-Indic digits: not ASCII hex
    ],
)
def test_parser_rejects_everything_else(ref):
    assert parse_target_ref(ref) is None


def test_parser_accepts_every_minted_device_id(store):
    # Cron's pass-through is strict once a parser is registered: an id the
    # parser rejects would silently drop that device's cron deliveries.
    for i in range(50):
        device_id, _rt = store.create_device(f"phone-{i}")
        assert parse_target_ref(device_id) == (device_id, None)


# ---------------------------------------------------------------------------
# validator
# ---------------------------------------------------------------------------


def test_validator_accepts_active_device(store):
    device_id, _ = store.create_device("iphone")
    assert make_target_validator(store)(device_id) is True


def test_validator_rejects_revoked_device_with_diagnostic(store):
    device_id, _ = store.create_device("iphone")
    store.revoke(device_id)
    verdict = make_target_validator(store)(device_id)
    assert isinstance(verdict, str)
    assert verdict == f"device {device_id} is revoked"


def test_validator_rejects_unknown_device_with_diagnostic(store):
    verdict = make_target_validator(store)(DEVICE_ID)
    assert isinstance(verdict, str)
    assert verdict == (f"no paired device {DEVICE_ID} — see `hermes mobile devices`")


def test_validator_reads_the_store_fresh_each_call(tmp_path):
    # The CLI (pair/revoke) is a separate process with its own DeviceStore; the
    # validator must see its writes without a restart.
    path = tmp_path / "devices.json"
    validate = make_target_validator(DeviceStore(path=path))
    other_process = DeviceStore(path=path)

    device_id, _ = other_process.create_device("iphone")
    assert validate(device_id) is True
    other_process.revoke(device_id)
    assert validate(device_id) == f"device {device_id} is revoked"


def test_validator_store_failure_is_a_diagnostic_not_an_exception(tmp_path):
    path = tmp_path / "devices.json"
    path.write_text("[]", encoding="utf-8")  # malformed store
    verdict = make_target_validator(DeviceStore(path=path))(DEVICE_ID)
    assert isinstance(verdict, str) and verdict
    assert "device store" in verdict


# ---------------------------------------------------------------------------
# registration — feature-detected PlatformEntry kwargs
# ---------------------------------------------------------------------------


class EntryBuildingCtx:
    """Mimics PluginContext.register_platform: kwargs → PlatformEntry(**kwargs).

    The real one forwards extra kwargs to the dataclass constructor, so an
    unknown key raises TypeError (hermes_cli/plugins.py register_platform).
    """

    def __init__(self, entry_cls):
        self.entry_cls = entry_cls
        self.kwargs: dict = {}
        self.entry = None

    def register_platform(self, **kwargs):
        self.kwargs = kwargs
        self.entry = self.entry_cls(**kwargs)


_NEW_FIELDS = ("parse_target_ref_fn", "validate_target_ref_fn", "standalone_sender_fn")


def test_registration_passes_target_fields_when_platform_entry_has_them(store):
    field_names = {f.name for f in dataclasses.fields(PlatformEntry)}
    if not set(_NEW_FIELDS) <= field_names:
        pytest.skip("this hermes core's PlatformEntry predates the target fields")
    ctx = EntryBuildingCtx(PlatformEntry)
    register_platform(ctx, store)

    entry = ctx.entry
    assert entry.cron_deliver_env_var == "MOBILE_HOME_CHANNEL"
    assert entry.parse_target_ref_fn(DEVICE_ID.upper()) == (DEVICE_ID, None)
    device_id, _ = store.create_device("iphone")
    assert entry.validate_target_ref_fn(device_id) is True
    assert entry.validate_target_ref_fn(DEVICE_ID) == (
        f"no paired device {DEVICE_ID} — see `hermes mobile devices`"
    )
    assert inspect.iscoroutinefunction(entry.standalone_sender_fn)


@dataclasses.dataclass
class _OldPlatformEntry:
    """A PlatformEntry from before the target-parsing / standalone-sender fields."""

    name: str
    label: str
    adapter_factory: Callable[[Any], Any]
    check_fn: Callable[[], bool]
    validate_config: Optional[Callable[[Any], bool]] = None
    required_env: list = dataclasses.field(default_factory=list)
    install_hint: str = ""
    source: str = "plugin"
    plugin_name: str = ""
    emoji: str = "🔌"
    platform_hint: str = ""
    cron_deliver_env_var: str = ""


def test_registration_omits_target_fields_on_an_older_platform_entry(
    store, monkeypatch
):
    monkeypatch.setattr(registry_mod, "PlatformEntry", _OldPlatformEntry)
    ctx = EntryBuildingCtx(_OldPlatformEntry)

    register_platform(ctx, store)  # must not raise TypeError

    for name in _NEW_FIELDS:
        assert name not in ctx.kwargs
    # Everything registered before this change is still passed.
    assert ctx.kwargs["name"] == "mobile"
    assert ctx.kwargs["cron_deliver_env_var"] == "MOBILE_HOME_CHANNEL"
    assert ctx.kwargs["platform_hint"]


def test_registration_passes_only_the_fields_that_exist(store, monkeypatch):
    @dataclasses.dataclass
    class _PartialEntry(_OldPlatformEntry):
        parse_target_ref_fn: Optional[Callable] = None
        validate_target_ref_fn: Optional[Callable] = None

    monkeypatch.setattr(registry_mod, "PlatformEntry", _PartialEntry)
    ctx = EntryBuildingCtx(_PartialEntry)

    register_platform(ctx, store)

    assert callable(ctx.kwargs["parse_target_ref_fn"])
    assert callable(ctx.kwargs["validate_target_ref_fn"])
    assert "standalone_sender_fn" not in ctx.kwargs


# ---------------------------------------------------------------------------
# standalone (out-of-process) sender
# ---------------------------------------------------------------------------


def test_standalone_sender_matches_the_core_contract():
    # async (pconfig, chat_id, message, *, thread_id=None, media_files=None,
    #        force_document=False) -> dict   (PlatformEntry.standalone_sender_fn)
    sender = make_standalone_sender(DeviceStore())
    assert inspect.iscoroutinefunction(sender)
    params = inspect.signature(sender).parameters
    assert list(params)[:3] == ["pconfig", "chat_id", "message"]
    for name, default in (
        ("thread_id", None),
        ("media_files", None),
        ("force_document", False),
    ):
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY
        assert params[name].default == default


def test_standalone_sender_delivers_through_the_adapter_send_path(tmp_path, store):
    device_id, _ = store.create_device("iphone")
    store.set_push_token(device_id, "ExponentPushToken[abc]")
    push = RecordingPush()
    sender = make_standalone_sender(store, push=push, mailbox_dir=tmp_path / "mailbox")

    result = run(sender(PlatformConfig(), device_id, "hello phone"))

    assert result["success"] is True
    lines = (tmp_path / "mailbox" / f"{device_id}.jsonl").read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["content"] == "hello phone"
    assert result["message_id"] == record["message_id"]
    # Same redacted push as the live adapter (content never goes to Expo).
    assert push.sent == [
        {"token": "ExponentPushToken[abc]", "title": "Hermes", "body": None}
    ]


def test_standalone_sender_accepts_the_contract_kwargs(tmp_path, store):
    device_id, _ = store.create_device("iphone")
    sender = make_standalone_sender(
        store, push=RecordingPush(), mailbox_dir=tmp_path / "mailbox"
    )
    result = run(
        sender(
            PlatformConfig(),
            device_id,
            "hi",
            thread_id=None,
            media_files=[],
            force_document=False,
        )
    )
    assert result["success"] is True


def test_standalone_sender_reports_adapter_failures_as_error_dicts(tmp_path, store):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    sender = make_standalone_sender(
        store, push=RecordingPush(), mailbox_dir=blocker / "mailbox"
    )
    result = run(sender(PlatformConfig(), DEVICE_ID, "hi"))
    assert set(result) == {"error"} and result["error"]

    bad_id = run(sender(PlatformConfig(), "../etc/passwd", "hi"))
    assert set(bad_id) == {"error"} and "invalid mobile device id" in bad_id["error"]


def test_standalone_sender_refuses_to_write_as_root_into_a_user_owned_tree(
    tmp_path, store, monkeypatch
):
    # `docker exec … hermes` drops to the gateway uid by default, but
    # HERMES_DOCKER_EXEC_AS_ROOT=1 keeps root. A root-created mailbox file or dir
    # (0600/0700) would lock the gateway and the dashboard drain out of it.
    monkeypatch.setattr(adapter_mod.os, "geteuid", lambda: 0)
    mailbox = tmp_path / "mailbox"  # tmp_path is owned by the (non-root) test user
    push = RecordingPush()
    sender = make_standalone_sender(store, push=push, mailbox_dir=mailbox)

    result = run(sender(PlatformConfig(), DEVICE_ID, "hi"))

    assert set(result) == {"error"}
    assert "root" in result["error"]
    assert not mailbox.exists()
    assert push.sent == []


def test_standalone_sender_root_check_is_a_noop_for_non_root(
    tmp_path, store, monkeypatch
):
    monkeypatch.setattr(adapter_mod.os, "geteuid", lambda: 1000)
    sender = make_standalone_sender(
        store, push=RecordingPush(), mailbox_dir=tmp_path / "mailbox"
    )
    assert run(sender(PlatformConfig(), DEVICE_ID, "hi"))["success"] is True


# ---------------------------------------------------------------------------
# the real core resolver
# ---------------------------------------------------------------------------


def _core_resolve_send_target():
    try:  # 0.21.x
        from tools.send_message_targets import resolve_send_target
    except ImportError:  # 0.20.x
        from tools.send_message_tool import resolve_send_target
    return resolve_send_target


@pytest.fixture
def registered_mobile(store, tmp_path, monkeypatch):
    """Register the plugin's real 'mobile' PlatformEntry in the core registry."""
    if not set(_NEW_FIELDS) <= {f.name for f in dataclasses.fields(PlatformEntry)}:
        pytest.skip("this hermes core's PlatformEntry predates target parsing")
    # Keep channel-directory fallback away from the developer's real ~/.hermes.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    previous = platform_registry.get("mobile")

    class RegistryCtx:
        def register_platform(self, **kwargs):
            platform_registry.register(
                PlatformEntry(source="plugin", plugin_name="hermes-mobile", **kwargs)
            )

    register_platform(RegistryCtx(), store)
    yield store
    platform_registry.unregister("mobile")
    if previous is not None:
        platform_registry.register(previous)


def test_core_resolves_an_active_device_id(registered_mobile):
    device_id, _ = registered_mobile.create_device("iphone")
    resolve = _core_resolve_send_target()
    assert resolve("mobile", device_id) == (device_id, None, None)
    assert resolve("mobile", f" {device_id.upper()} ") == (device_id, None, None)


def test_core_rejects_revoked_and_unknown_ids_with_our_diagnostics(registered_mobile):
    device_id, _ = registered_mobile.create_device("iphone")
    registered_mobile.revoke(device_id)
    resolve = _core_resolve_send_target()

    chat_id, thread_id, error = resolve("mobile", device_id)
    assert (chat_id, thread_id) == (None, None)
    assert error.endswith(f"device {device_id} is revoked")

    chat_id, thread_id, error = resolve("mobile", DEVICE_ID)
    assert (chat_id, thread_id) == (None, None)
    assert error.endswith(f"no paired device {DEVICE_ID} — see `hermes mobile devices`")


def test_core_cron_pass_through_accepts_device_ids_and_stays_strict(registered_mobile):
    # cron/scheduler_delivery.py resolves deliver=mobile:<id> with
    # pass_unresolved_references=True; with our parser registered that path is
    # strict, so a real id must resolve and a non-id must not reach the adapter.
    device_id, _ = registered_mobile.create_device("iphone")
    resolve = _core_resolve_send_target()

    assert resolve("mobile", device_id, pass_unresolved_references=True) == (
        device_id,
        None,
        None,
    )
    chat_id, _thread, error = resolve(
        "mobile", "my-iphone", pass_unresolved_references=True
    )
    assert chat_id is None and error


def test_core_out_of_process_send_uses_our_standalone_sender(
    registered_mobile, tmp_path
):
    # `hermes send` runs without a gateway runner in its process, so the core's
    # _send_via_adapter finds no live adapter and must fall back to
    # PlatformEntry.standalone_sender_fn — the mailbox lands under HERMES_HOME.
    from gateway.config import Platform
    from tools.send_message_tool import _send_via_adapter

    device_id, _ = registered_mobile.create_device("iphone")
    result = run(
        _send_via_adapter(
            Platform("mobile"), PlatformConfig(enabled=True), device_id, "hello"
        )
    )

    assert result["success"] is True
    mailbox = tmp_path / "hermes-home" / "mobile" / "mailbox" / f"{device_id}.jsonl"
    record = json.loads(mailbox.read_text())
    assert record["content"] == "hello"
    assert result["message_id"] == record["message_id"]
