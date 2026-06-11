"""Tests for hermes_mobile.mailbox — per-device JSONL append/drain."""

from __future__ import annotations

import json
import stat

import pytest

from hermes_mobile import mailbox


def test_append_writes_jsonl_record(tmp_path):
    record = mailbox.append_message(tmp_path, "abcd1234", "hello", clock=lambda: 1000.5)
    path = tmp_path / "abcd1234.jsonl"
    assert path.exists()
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    on_disk = json.loads(lines[0])
    assert on_disk == record
    assert on_disk["ts"] == 1000.5
    assert on_disk["chat_id"] == "abcd1234"
    assert on_disk["content"] == "hello"
    assert on_disk["message_id"]


def test_append_is_append_only_and_private(tmp_path):
    mailbox.append_message(tmp_path, "dev1", "one")
    mailbox.append_message(tmp_path, "dev1", "two")
    path = tmp_path / "dev1.jsonl"
    contents = [json.loads(l)["content"] for l in path.read_text().splitlines()]
    assert contents == ["one", "two"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_append_optional_fields(tmp_path):
    record = mailbox.append_message(
        tmp_path, "dev1", "x", reply_to="m1", metadata={"k": "v"}
    )
    assert record["reply_to"] == "m1"
    assert record["metadata"] == {"k": "v"}


def test_append_rejects_path_traversal_ids(tmp_path):
    for bad in ("../evil", "a/b", "", "x" * 65, "dev\n1", ".."):
        with pytest.raises(ValueError):
            mailbox.append_message(tmp_path, bad, "x")


def test_drain_returns_and_empties(tmp_path):
    mailbox.append_message(tmp_path, "dev1", "one")
    mailbox.append_message(tmp_path, "dev1", "two")
    msgs = mailbox.drain_messages(tmp_path, "dev1")
    assert [m["content"] for m in msgs] == ["one", "two"]
    # Drained: second call is empty, file gone.
    assert mailbox.drain_messages(tmp_path, "dev1") == []
    assert not (tmp_path / "dev1.jsonl").exists()


def test_drain_missing_mailbox(tmp_path):
    assert mailbox.drain_messages(tmp_path, "dev1") == []


def test_drain_skips_corrupt_lines(tmp_path):
    mailbox.append_message(tmp_path, "dev1", "good")
    path = tmp_path / "dev1.jsonl"
    with path.open("a") as fh:
        fh.write("not-json\n")
        fh.write('"a bare string"\n')
    msgs = mailbox.drain_messages(tmp_path, "dev1")
    assert len(msgs) == 1
    assert msgs[0]["content"] == "good"


def test_drain_rejects_unsafe_ids(tmp_path):
    with pytest.raises(ValueError):
        mailbox.drain_messages(tmp_path, "../../etc/passwd")


def test_default_mailbox_dir_is_sibling_of_devices_json():
    from hermes_mobile.device_store import default_devices_path

    assert mailbox.default_mailbox_dir() == default_devices_path().parent / "mailbox"
