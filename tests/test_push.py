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


# ---------------------------------------------------------------------------
# token redaction in logs
# ---------------------------------------------------------------------------

_TOKEN = "ExponentPushToken[xXsecretTOKEN123]"


def _log_text(caplog) -> str:
    return "\n".join(f"{r.getMessage()} {r.args!r}" for r in caplog.records)


def test_ticket_error_log_redacts_the_push_token(caplog):
    # Expo's DeviceNotRegistered message embeds the token verbatim.
    transport = RecordingTransport(
        response=json.dumps(
            {
                "data": {
                    "status": "error",
                    "message": f'"{_TOKEN}" is not a registered push notification recipient',
                    "details": {"error": "DeviceNotRegistered"},
                }
            }
        )
    )
    with caplog.at_level("WARNING"):
        assert ExpoPush(transport=transport).send(_TOKEN) is False
    text = _log_text(caplog)
    assert caplog.records
    assert "xXsecretTOKEN123" not in text
    assert "DeviceNotRegistered" in text
    assert "[redacted]" in text


def test_http_error_body_log_redacts_push_tokens(caplog):
    body = json.dumps(
        {
            "errors": [
                {"message": "bad ExpoPushToken[otherSECRET] and ExponentPushToken[x]"}
            ]
        }
    )
    transport = RecordingTransport(status=400, response=body)
    with caplog.at_level("WARNING"):
        assert ExpoPush(transport=transport).send(_TOKEN) is False
    text = _log_text(caplog)
    assert caplog.records
    assert "otherSECRET" not in text
    assert "ExponentPushToken[x]" not in text


def test_error_logs_redact_the_devices_own_bare_token(caplog):
    # A token without the Expo wrapper is still the device's own secret.
    bare = "fcm-bare-token-SECRET-42"
    transport = RecordingTransport(
        response=json.dumps({"data": {"status": "error", "message": f"bad {bare}"}})
    )
    with caplog.at_level("WARNING"):
        assert ExpoPush(transport=transport).send(bare) is False
    http = RecordingTransport(status=500, response=f"upstream choked on {bare}")
    with caplog.at_level("WARNING"):
        assert ExpoPush(transport=http).send(bare) is False
    net = RecordingTransport(exc=OSError(f"reset while sending {bare}"))
    with caplog.at_level("WARNING"):
        assert ExpoPush(transport=net).send(bare) is False
    text = _log_text(caplog)
    assert len(caplog.records) == 3
    assert bare not in text


# ---------------------------------------------------------------------------
# redaction hardening — one case per shape from the PR #10 round-2 probe
# (scratchpad/pr10/redact_probe.py). Every secret below contains "secret".
# ---------------------------------------------------------------------------

_PROBE_TOKEN = "ExponentPushToken[AAAAsecretBBBB]"


def _t(status=200, body="", exc=None):
    def transport(url, data, headers):
        if exc is not None:
            raise exc
        return status, body

    return transport


_ESCAPED_BODY = (
    json.dumps({"m": _PROBE_TOKEN}, ensure_ascii=True)
    .replace("[", "\\u005b")
    .replace("]", "\\u005d")
)

_PROBE_CASES = {
    "ticket, device's own token": (
        _PROBE_TOKEN,
        _t(
            body=json.dumps(
                {
                    "data": {
                        "status": "error",
                        "message": f'"{_PROBE_TOKEN}" not registered',
                    }
                }
            )
        ),
    ),
    "ticket, token in details only": (
        _PROBE_TOKEN,
        _t(
            body=json.dumps(
                {
                    "data": {
                        "status": "error",
                        "message": "x",
                        "details": {
                            "error": "DeviceNotRegistered",
                            "expoPushToken": _PROBE_TOKEN,
                        },
                    }
                }
            )
        ),
    ),
    "http body, truncated token (no closing bracket)": (
        _PROBE_TOKEN,
        _t(400, 'bad "to": ExponentPushToken[AAAAsecretBB'),
    ),
    "http body, JSON-escaped brackets": (_PROBE_TOKEN, _t(400, _ESCAPED_BODY)),
    "http body, JSON-escaped brackets, uppercase hex": (
        _PROBE_TOKEN,
        _t(400, _ESCAPED_BODY.replace("005b", "005B").replace("005d", "005D")),
    ),
    "http body, other device's token, lowercase prefix": (
        _PROBE_TOKEN,
        _t(400, "bad exponentpushtoken[OTHERsecretX]"),
    ),
    "http body, other device's ExpoPushToken, upper case": (
        _PROBE_TOKEN,
        _t(400, "bad EXPOPUSHTOKEN[OTHERsecretX]"),
    ),
    "http body, other device's token, space before bracket": (
        _PROBE_TOKEN,
        _t(400, "bad ExponentPushToken [OTHERsecretX]"),
    ),
    "http body, token past the 200-char cut": (
        _PROBE_TOKEN,
        _t(400, "x" * 190 + _PROBE_TOKEN),
    ),
    "ticket list, second ticket": (
        _PROBE_TOKEN,
        _t(
            body=json.dumps(
                {
                    "data": [
                        {"status": "ok"},
                        {"status": "error", "message": _PROBE_TOKEN},
                    ]
                }
            )
        ),
    ),
    "network exception containing the token": (
        _PROBE_TOKEN,
        _t(exc=OSError(f"reset {_PROBE_TOKEN}")),
    ),
    "ticket code is the token": (
        _PROBE_TOKEN,
        _t(
            body=json.dumps(
                {
                    "data": {
                        "status": "error",
                        "message": "m",
                        "details": {"error": _PROBE_TOKEN},
                    }
                }
            )
        ),
    ),
    "http 413 echoing a truncated prefix of the own bare token": (
        "fcmTOKENsecretXYZ",
        _t(413, "rejected fcmTOKENsecr"),
    ),
}


@pytest.mark.parametrize("case", list(_PROBE_CASES), ids=list(_PROBE_CASES))
def test_probe_case_logs_no_token(case, caplog):
    token, transport = _PROBE_CASES[case]
    with caplog.at_level("WARNING", logger="hermes_mobile.push"):
        assert ExpoPush(transport=transport).send(token) is False
    assert caplog.records, "expected a WARNING for the failed push"
    text = _log_text(caplog)
    assert "secret" not in text.lower(), text
    # A truncated token keeps only a prefix of "secret" (e.g. "fcmTOKENsecr").
    assert "secr" not in text.lower(), text


def test_ticket_details_error_code_is_redacted(caplog):
    # R2-2: the logged details.error code goes through the same redaction.
    bare = "fcm-bare-token-secret-77"
    transport = _t(
        body=json.dumps(
            {"data": {"status": "error", "message": "m", "details": {"error": bare}}}
        )
    )
    with caplog.at_level("WARNING", logger="hermes_mobile.push"):
        assert ExpoPush(transport=transport).send(bare) is False
    text = _log_text(caplog)
    assert "secret" not in text.lower(), text
    assert "(" + "[redacted]" + ")" in text


def test_redaction_leaves_ordinary_error_text_alone(caplog):
    # The loosened pattern must not eat unrelated words or the error code.
    transport = _t(
        body=json.dumps(
            {
                "data": {
                    "status": "error",
                    "message": "Expo push service unavailable, retry later",
                    "details": {"error": "MessageRateExceeded"},
                }
            }
        )
    )
    with caplog.at_level("WARNING", logger="hermes_mobile.push"):
        assert ExpoPush(transport=transport).send("fcmTOKENsecretXYZ") is False
    text = _log_text(caplog)
    assert "Expo push service unavailable, retry later" in text
    assert "MessageRateExceeded" in text
