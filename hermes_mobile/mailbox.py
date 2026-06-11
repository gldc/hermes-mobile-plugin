"""Per-device mailbox — append-only JSONL files, drained by the app.

One file per device at ``<hermes home>/mobile/mailbox/<device_id>.jsonl``
(base directory injectable for tests). The mailbox is the source of
truth for agent-initiated messages; Expo push is only the best-effort
"go look" signal. Pure stdlib so it is shared by the gateway adapter
(append) and the dashboard plugin API (drain) without import baggage.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from .device_store import default_devices_path

#: device ids are secrets.token_hex(8); anything outside this set is
#: rejected to keep chat_id from ever becoming a path component attack.
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def default_mailbox_dir() -> Path:
    """``<hermes home>/mobile/mailbox`` (sibling of devices.json)."""
    return default_devices_path().parent / "mailbox"


def is_safe_device_id(device_id: str) -> bool:
    return bool(isinstance(device_id, str) and _SAFE_ID_RE.match(device_id))


def _mailbox_path(base_dir: Path, device_id: str) -> Path:
    if not is_safe_device_id(device_id):
        raise ValueError(f"invalid device id for mailbox path: {device_id!r}")
    return Path(base_dir) / f"{device_id}.jsonl"


def append_message(
    base_dir: Path,
    device_id: str,
    content: str,
    *,
    reply_to: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    clock=time.time,
) -> Dict[str, Any]:
    """Append one message record; returns the record (incl. message_id)."""
    path = _mailbox_path(base_dir, device_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    record: Dict[str, Any] = {
        "ts": float(clock()),
        "chat_id": device_id,
        "content": str(content),
        "message_id": uuid.uuid4().hex,
    }
    if reply_to:
        record["reply_to"] = reply_to
    if metadata:
        record["metadata"] = metadata
    line = json.dumps(record, separators=(",", ":")) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    return record


def drain_messages(base_dir: Path, device_id: str) -> List[Dict[str, Any]]:
    """Return all queued messages for *device_id* and empty the mailbox.

    Missing mailbox → empty list. Unparseable lines are skipped (the
    file is process-private; corruption should not wedge the sync loop).
    """
    path = _mailbox_path(base_dir, device_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    messages: List[Dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            messages.append(obj)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return messages
