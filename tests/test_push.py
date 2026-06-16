"""Tests for hermes_mobile.push (ExpoPush) and device-store push tokens."""

from __future__ import annotations

import json

import pytest

from hermes_mobile.device_store import DeviceStore
from hermes_mobile.push import DEFAULT_BODY, DEFAULT_TITLE, EXPO_PUSH_URL, ExpoPush


class RecordingTransport:
    def __init__(self, status=200, response='{"data":{"status":"ok"}}', exc=None):
        self.status = status
        self.response = response
        self.exc = exc
        self.calls = []

    def __call__(self, url, data, headers):
        self.calls.append({"url": url, "data": data, "headers": headers})
        if self.exc is not None:
            raise self.exc
        return self.status, self.response


# ---------------------------------------------------------------------------
# ExpoPush
# ---------------------------------------------------------------------------


def test_send_posts_to_expo_with_redacted_default_body():
    transport = RecordingTransport()
    ok = ExpoPush(transport=transport).send("ExponentPushToken[abc]")
    assert ok is True
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["url"] == EXPO_PUSH_URL
    assert call["headers"]["Content-Type"] == "application/json"
    payload = json.loads(call["data"].decode("utf-8"))
    assert payload == {
        "to": "ExponentPushToken[abc]",
        "title": DEFAULT_TITLE,
        "body": DEFAULT_BODY,
    }


def test_send_explicit_title_and_body():
    transport = RecordingTransport()
    ExpoPush(transport=transport).send("tok", title="T", body="preview text")
    payload = json.loads(transport.calls[0]["data"].decode("utf-8"))
    assert payload["title"] == "T"
    assert payload["body"] == "preview text"


def test_send_empty_token_is_noop():
    transport = RecordingTransport()
    assert ExpoPush(transport=transport).send("") is False
    assert transport.calls == []


def test_send_network_failure_never_raises(caplog):
    transport = RecordingTransport(exc=OSError("connection refused"))
    with caplog.at_level("WARNING"):
        ok = ExpoPush(transport=transport).send("tok")
    assert ok is False
    assert any("Expo push failed" in r.message for r in caplog.records)


def test_send_http_error_returns_false(caplog):
    transport = RecordingTransport(status=429, response="rate limited")
    with caplog.at_level("WARNING"):
        assert ExpoPush(transport=transport).send("tok") is False


def test_send_expo_ticket_error_returns_false(caplog):
    transport = RecordingTransport(
        response=json.dumps(
            {"data": {"status": "error", "message": "DeviceNotRegistered"}}
        )
    )
    with caplog.at_level("WARNING"):
        assert ExpoPush(transport=transport).send("tok") is False
    assert any("DeviceNotRegistered" in r.message for r in caplog.records)


def test_send_list_shaped_ticket_ok():
    transport = RecordingTransport(response='{"data":[{"status":"ok","id":"x"}]}')
    assert ExpoPush(transport=transport).send("tok") is True


def test_send_garbage_response_returns_false():
    transport = RecordingTransport(response="<html>not json</html>")
    assert ExpoPush(transport=transport).send("tok") is False


def test_send_includes_data_when_provided():
    captured = {}

    def transport(url, body, headers):
        captured["payload"] = json.loads(body.decode("utf-8"))
        return 200, json.dumps({"data": {"status": "ok"}})

    ExpoPush(transport=transport).send(
        "ExponentPushToken[x]", body="ready", data={"type": "session_end"}
    )
    assert captured["payload"]["data"] == {"type": "session_end"}


def test_send_omits_data_key_when_none():
    captured = {}

    def transport(url, body, headers):
        captured["payload"] = json.loads(body.decode("utf-8"))
        return 200, json.dumps({"data": {"status": "ok"}})

    ExpoPush(transport=transport).send("ExponentPushToken[x]")
    assert "data" not in captured["payload"]


# ---------------------------------------------------------------------------
# DeviceStore push-token persistence
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path) -> DeviceStore:
    return DeviceStore(path=tmp_path / "devices.json")


def test_set_and_get_push_token(store):
    device_id, _ = store.create_device("phone")
    assert store.get_push_token(device_id) is None
    assert store.set_push_token(device_id, "ExponentPushToken[xyz]") is True
    assert store.get_push_token(device_id) == "ExponentPushToken[xyz]"
    # Refresh overwrites.
    assert store.set_push_token(device_id, "ExponentPushToken[new]") is True
    assert store.get_push_token(device_id) == "ExponentPushToken[new]"


def test_push_token_unknown_device(store):
    assert store.set_push_token("nope", "tok") is False
    assert store.get_push_token("nope") is None


def test_push_token_revoked_device(store):
    device_id, _ = store.create_device("phone")
    store.set_push_token(device_id, "tok")
    store.revoke(device_id)
    assert store.get_push_token(device_id) is None
    assert store.set_push_token(device_id, "tok2") is False


def test_push_token_survives_rotation(store):
    device_id, rt = store.create_device("phone")
    store.set_push_token(device_id, "tok")
    store.rotate_refresh(rt)
    assert store.get_push_token(device_id) == "tok"


def test_get_device(store):
    device_id, _ = store.create_device("phone")
    record = store.get_device(device_id)
    assert record is not None
    assert record["device_id"] == device_id
    assert record["name"] == "phone"
    assert store.get_device("nope") is None


def test_legacy_record_without_push_token_field(store):
    # Records written before push_token existed must still read cleanly.
    device_id, _ = store.create_device("old")
    raw = json.loads(store._path.read_text())
    del raw["devices"][device_id]["push_token"]
    store._path.write_text(json.dumps(raw))
    assert store.get_push_token(device_id) is None
    assert store.set_push_token(device_id, "tok") is True
    assert store.get_push_token(device_id) == "tok"
