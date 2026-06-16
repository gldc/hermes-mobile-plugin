# Session-Stop Push Notifications Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Push paired phones a redacted "come back" notification when a mobile-originated agent run stops or needs approval (and when a cron run finishes), suppressed on whatever device is foreground.

**Architecture (revised after adversarial review):** The app submits prompts over `/api/ws` → **tui_gateway**, where `platform="tui"` and there is **no `mobile:` identity** on the session. So the plugin learns the device→session link itself: the app calls a plugin-owned authenticated `POST /api/plugins/mobile/session-claim {session_id, session_key}` right after `session.create`/`session.resume` (it already holds both — `session_id`=`liveIdRef`, `session_key`=`stored_session_id`=`storedIdRef`, which equals the tui `session_key` the hooks carry). The route reads the device via the existing `_require_device_id` and records `{session_id, session_key} → device_id` in an in-process `SessionClaimRegistry`. The `on_session_end` and `pre_approval_request` hooks (both verified to fire on this path) resolve the registry and fan out a redacted Expo push with a `data.type`. The app suppresses the banner for those types while foreground. No hermes-agent fork.

**Tech Stack:** Python 3 + pytest (plugin; `PYTHONPATH=$HOME/Developer/hermes-agent python -m pytest tests/ -q`); TypeScript + Expo SDK 56 + jest (app; `npx tsc --noEmit`, `npx jest`).

**Spec:** `docs/plans/session-stop-push-design.md`.

**Branches:** plugin `feat/session-stop-push` (exists); app `feat/session-stop-push` (create in Part B).

**Verified APIs (do not re-guess):**
- `ExpoPush.send(token, title=DEFAULT_TITLE, body=None)` — `push.py`; payload `{to,title,body}`.
- `DeviceStore`: `create_device(name)->(device_id, rt)`, `set_push_token(device_id, token)`, `revoke(device_id)`, `list_devices()->list[dict]` (records carry `revoked` and verbatim `push_token`), `DeviceStore(path=...)`.
- `plugin_api.py`: `router = APIRouter()`, `configure(store=...)` injects `_store`, `_get_store()`, `_require_device_id(request)->device_id` (403 otherwise), pydantic `BaseModel` bodies, route style `@router.post("/x") def f(body, request) -> Dict`.
- Hooks fire with: `on_session_end(session_id, task_id, turn_id, completed, interrupted, model, platform, telemetry_schema_version=…)`; `pre_approval_request(command, description, pattern_key, pattern_keys, session_key, surface, turn_id, tool_call_id, telemetry_schema_version=…)`. Always use `**_` to absorb injected kwargs.
- Cron: `HERMES_CRON_SESSION == "1"` is a real env var (`os.getenv` ok). `HERMES_CRON_AUTO_DELIVER_PLATFORM` is a **ContextVar** — read via `gateway.session_context.get_session_env(name, default)` (lazy import; gateway only present in the gateway process).

---

# Part A — Plugin (`~/Developer/hermes-mobile-plugin`, branch `feat/session-stop-push`)

> Commands from `~/Developer/hermes-mobile-plugin`. Test: `PYTHONPATH=$HOME/Developer/hermes-agent python -m pytest tests/ -q`.

## Task A1: `ExpoPush.send` accepts an optional `data` dict

**Files:** Modify `hermes_mobile/push.py`; Test `tests/test_push.py`.

- [ ] **Step 1: Failing tests** — add to `tests/test_push.py`:

```python
def test_send_includes_data_when_provided():
    captured = {}
    def transport(url, body, headers):
        captured["payload"] = json.loads(body.decode("utf-8"))
        return 200, json.dumps({"data": {"status": "ok"}})
    ExpoPush(transport=transport).send("ExponentPushToken[x]", body="ready", data={"type": "session_end"})
    assert captured["payload"]["data"] == {"type": "session_end"}

def test_send_omits_data_key_when_none():
    captured = {}
    def transport(url, body, headers):
        captured["payload"] = json.loads(body.decode("utf-8"))
        return 200, json.dumps({"data": {"status": "ok"}})
    ExpoPush(transport=transport).send("ExponentPushToken[x]")
    assert "data" not in captured["payload"]
```

- [ ] **Step 2: Run, verify fail** — `PYTHONPATH=$HOME/Developer/hermes-agent python -m pytest tests/test_push.py -q -k data` → FAIL (`unexpected keyword 'data'`).
- [ ] **Step 3: Implement** — add `data: Optional[dict] = None` to `send`'s signature; after building `payload`, `if data is not None: payload["data"] = data`. Update the docstring to note `data` carries routing only (no content).
- [ ] **Step 4: Run, verify pass** — `... pytest tests/test_push.py -q` → PASS.
- [ ] **Step 5: Commit** — `git add hermes_mobile/push.py tests/test_push.py && git commit -m "feat(push): ExpoPush.send accepts optional data dict"`

---

## Task A2: `SessionClaimRegistry`

**Files:** Create `hermes_mobile/session_notify.py` (registry only this task); Test `tests/test_session_notify.py`.

- [ ] **Step 1: Failing tests** — create `tests/test_session_notify.py`:

```python
from hermes_mobile.session_notify import SessionClaimRegistry


def test_claim_and_resolve_by_either_id():
    clock = {"t": 1000.0}
    reg = SessionClaimRegistry(ttl_seconds=100, clock=lambda: clock["t"])
    reg.claim("dev1", "SID", "SKEY")
    assert reg.resolve("SID") == "dev1"
    assert reg.resolve("SKEY") == "dev1"
    assert reg.resolve("nope") is None
    assert reg.resolve(None, "", "SID") == "dev1"  # skips falsy, finds the hit


def test_ttl_eviction():
    clock = {"t": 1000.0}
    reg = SessionClaimRegistry(ttl_seconds=100, clock=lambda: clock["t"])
    reg.claim("dev1", "SID")
    clock["t"] = 1101.0  # past ttl
    assert reg.resolve("SID") is None


def test_claim_without_device_is_noop():
    reg = SessionClaimRegistry()
    reg.claim("", "SID")
    assert reg.resolve("SID") is None
```

- [ ] **Step 2: Run, verify fail** — `... pytest tests/test_session_notify.py -q` → FAIL (no module).
- [ ] **Step 3: Implement** — create `hermes_mobile/session_notify.py` with ONLY the registry for now (rest added in A3). No gateway imports at module top:

```python
"""Session-stop push notifications (docs/plans/session-stop-push-design.md).

Pings paired devices when a mobile-originated run stops / needs approval, or a
cron run finishes. Device attribution comes from a plugin-owned session-claim
route (the app calls it after session.create/resume); the hooks resolve the
resulting in-process registry. No gateway import at module top, so this loads in
every host process; gateway-only helpers are imported lazily. Best-effort:
failures are logged and never affect the agent run.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import List, Optional

from .device_store import DeviceStore
from .push import ExpoPush

logger = logging.getLogger(__name__)

SESSION_END_BODY = "Your session is ready — tap to check"
APPROVAL_BODY = "Hermes needs your approval"
_DISABLED_VALUES = {"0", "false", "no", "off"}
_DEFAULT_TTL_SECONDS = 24 * 60 * 60


class SessionClaimRegistry:
    """In-process, thread-safe TTL map: session_id / session_key -> device_id."""

    def __init__(self, ttl_seconds: int = _DEFAULT_TTL_SECONDS, clock=time.monotonic) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._by_id: dict[str, tuple[str, float]] = {}

    def claim(self, device_id: str, *ids: Optional[str]) -> None:
        if not device_id:
            return
        expires = self._clock() + self._ttl
        with self._lock:
            for i in ids:
                if i:
                    self._by_id[str(i)] = (device_id, expires)

    def resolve(self, *ids: Optional[str]) -> Optional[str]:
        now = self._clock()
        with self._lock:
            for i in ids:
                if not i:
                    continue
                hit = self._by_id.get(str(i))
                if hit is not None and hit[1] > now:
                    return hit[0]
            return None


_registry = SessionClaimRegistry()


def get_registry() -> SessionClaimRegistry:
    """The process-wide registry shared by the session-claim route and hooks."""
    return _registry
```

- [ ] **Step 4: Run, verify pass** — `... pytest tests/test_session_notify.py -q` → PASS (3).
- [ ] **Step 5: Commit** — `git add hermes_mobile/session_notify.py tests/test_session_notify.py && git commit -m "feat(notify): SessionClaimRegistry (TTL session->device map)"`

---

## Task A3: `SessionNotifier` (qualify + fan-out)

**Files:** Modify `hermes_mobile/session_notify.py`; Test `tests/test_session_notify.py`.

- [ ] **Step 1: Failing tests** — append to `tests/test_session_notify.py`:

```python
import pytest
from hermes_mobile.device_store import DeviceStore
from hermes_mobile.session_notify import SessionNotifier, get_registry


class RecordingPush:
    def __init__(self):
        self.sent = []
    def send(self, token, title="Hermes", body=None, data=None):
        self.sent.append({"token": token, "title": title, "body": body, "data": data})
        return True


@pytest.fixture
def store(tmp_path):
    return DeviceStore(path=tmp_path / "devices.json")


@pytest.fixture(autouse=True)
def _clear_registry():
    get_registry()._by_id.clear()
    yield
    get_registry()._by_id.clear()


def _tokened(store, name="phone", token="ExponentPushToken[abc]"):
    device_id, _ = store.create_device(name)
    store.set_push_token(device_id, token)
    return device_id


def test_session_end_pushes_for_claimed_session(store):
    _tokened(store)
    push = RecordingPush()
    get_registry().claim("dev-x", "SID", "SKEY")
    n = SessionNotifier(store=store, push=push, registry=get_registry())
    n.on_session_end(session_id="SID", task_id="SKEY", interrupted=False)
    assert len(push.sent) == 1
    assert push.sent[0]["data"] == {"type": "session_end"}
    assert push.sent[0]["body"] == "Your session is ready — tap to check"


def test_session_end_skips_unclaimed_session(store):
    _tokened(store)
    push = RecordingPush()
    n = SessionNotifier(store=store, push=push, registry=get_registry())
    n.on_session_end(session_id="UNCLAIMED", task_id="X", interrupted=False)
    assert push.sent == []  # browser/CLI/subagent never claimed -> excluded


def test_session_end_skips_interrupted(store):
    _tokened(store)
    push = RecordingPush()
    get_registry().claim("dev-x", "SID")
    n = SessionNotifier(store=store, push=push, registry=get_registry())
    n.on_session_end(session_id="SID", task_id="SID", interrupted=True)
    assert push.sent == []


def test_session_end_pushes_for_cron(store, monkeypatch):
    _tokened(store)
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    push = RecordingPush()
    n = SessionNotifier(store=store, push=push, registry=get_registry())
    n.on_session_end(session_id="whatever", task_id="x", interrupted=False)
    assert len(push.sent) == 1


def test_cron_dedup_when_delivered_to_mobile(store, monkeypatch):
    _tokened(store)
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    push = RecordingPush()
    n = SessionNotifier(store=store, push=push, registry=get_registry())
    # Patch the dedup reader directly (it reads a ContextVar via get_session_env).
    monkeypatch.setattr(
        "hermes_mobile.session_notify._already_delivered_to_mobile", lambda: True
    )
    n.on_session_end(session_id="whatever", task_id="x", interrupted=False)
    assert push.sent == []


def test_disabled_toggle(store, monkeypatch):
    _tokened(store)
    monkeypatch.setenv("MOBILE_NOTIFY_ON_SESSION_END", "0")
    push = RecordingPush()
    get_registry().claim("dev-x", "SID")
    n = SessionNotifier(store=store, push=push, registry=get_registry())
    n.on_session_end(session_id="SID", task_id="SID", interrupted=False)
    assert push.sent == []


def test_approval_pushes_for_claimed_gateway_session(store):
    _tokened(store)
    push = RecordingPush()
    get_registry().claim("dev-x", "SID", "SKEY")
    n = SessionNotifier(store=store, push=push, registry=get_registry())
    n.on_pre_approval_request(session_key="SKEY", surface="gateway")
    assert len(push.sent) == 1
    assert push.sent[0]["data"] == {"type": "approval_request"}
    assert push.sent[0]["body"] == "Hermes needs your approval"


def test_approval_skips_non_gateway_surface(store):
    _tokened(store)
    push = RecordingPush()
    get_registry().claim("dev-x", "SKEY")
    n = SessionNotifier(store=store, push=push, registry=get_registry())
    n.on_pre_approval_request(session_key="SKEY", surface="cli")
    assert push.sent == []


def test_fan_out_skips_revoked_and_tokenless(store):
    _tokened(store, name="good")
    store.create_device("no-token")
    rid = _tokened(store, name="revoked", token="ExponentPushToken[r]")
    store.revoke(rid)
    push = RecordingPush()
    get_registry().claim("dev-x", "SID")
    n = SessionNotifier(store=store, push=push, registry=get_registry())
    n.on_session_end(session_id="SID", task_id="SID", interrupted=False)
    assert len(push.sent) == 1
    assert push.sent[0]["token"] == "ExponentPushToken[abc]"


def test_absorbs_injected_kwargs(store):
    _tokened(store)
    push = RecordingPush()
    get_registry().claim("dev-x", "SID")
    n = SessionNotifier(store=store, push=push, registry=get_registry())
    # invoke_hook injects telemetry_schema_version etc.
    n.on_session_end(session_id="SID", task_id="SID", interrupted=False,
                     turn_id="t", completed=True, model="m", platform="tui",
                     telemetry_schema_version=1)
    assert len(push.sent) == 1
```

- [ ] **Step 2: Run, verify fail** — `... pytest tests/test_session_notify.py -q` → FAIL (`SessionNotifier` undefined).
- [ ] **Step 3: Implement** — append to `hermes_mobile/session_notify.py`:

```python
def _enabled() -> bool:
    return os.getenv("MOBILE_NOTIFY_ON_SESSION_END", "1").strip().lower() not in _DISABLED_VALUES


def _is_cron_run() -> bool:
    return os.getenv("HERMES_CRON_SESSION", "").strip() == "1"


def _already_delivered_to_mobile() -> bool:
    """HERMES_CRON_AUTO_DELIVER_PLATFORM is a ContextVar, not an env var — read it
    via the gateway's session-context accessor (gateway is present in the gateway
    process where cron's on_session_end fires). Lazy import keeps this module
    gateway-free at import time."""
    try:
        from gateway.session_context import get_session_env
    except Exception:
        return False
    return str(get_session_env("HERMES_CRON_AUTO_DELIVER_PLATFORM", "") or "").strip().lower() == "mobile"


class SessionNotifier:
    def __init__(self, store: Optional[DeviceStore] = None,
                 push: Optional[ExpoPush] = None,
                 registry: Optional[SessionClaimRegistry] = None) -> None:
        self._store = store if store is not None else DeviceStore()
        self._push = push if push is not None else ExpoPush()
        self._registry = registry if registry is not None else get_registry()

    def on_session_end(self, session_id: Optional[str] = None,
                       task_id: Optional[str] = None,
                       interrupted: bool = False, **_) -> None:
        if not _enabled() or interrupted:
            return
        if _is_cron_run():
            if _already_delivered_to_mobile():
                return
        elif self._registry.resolve(session_id, task_id) is None:
            return
        self._fan_out(SESSION_END_BODY, "session_end")

    def on_pre_approval_request(self, session_key: Optional[str] = None,
                                surface: Optional[str] = None, **_) -> None:
        if not _enabled() or surface != "gateway":
            return
        if self._registry.resolve(session_key) is None:
            return
        self._fan_out(APPROVAL_BODY, "approval_request")

    def _tokened_devices(self) -> List[dict]:
        try:
            return [d for d in self._store.list_devices()
                    if not d.get("revoked") and d.get("push_token")]
        except Exception:
            logger.debug("hermes-mobile: list_devices failed", exc_info=True)
            return []

    def _fan_out(self, body: str, notif_type: str) -> None:
        for d in self._tokened_devices():
            try:
                self._push.send(d["push_token"], body=body, data={"type": notif_type})
            except Exception:
                logger.debug("hermes-mobile: push send failed", exc_info=True)
```

- [ ] **Step 4: Run, verify pass** — `... pytest tests/test_session_notify.py -q` → PASS (all).
- [ ] **Step 5: Commit** — `git add hermes_mobile/session_notify.py tests/test_session_notify.py && git commit -m "feat(notify): SessionNotifier qualification + fan-out"`

---

## Task A4: `POST /session-claim` route

**Files:** Modify `hermes_mobile/plugin_api.py`; Test `tests/test_plugin_api.py`.

- [ ] **Step 1: Failing test** — add to `tests/test_plugin_api.py`, mirroring the existing route tests' auth/session setup (find how they set `request.state.session`; reuse that fixture/helper). Assert that a claim records the ids into the registry:

```python
def test_session_claim_records_device(client_with_device):
    # client_with_device: existing helper that issues requests as a verified
    # mobile-device session for a known device_id (mirror the push-token test).
    from hermes_mobile.session_notify import get_registry
    get_registry()._by_id.clear()
    resp = client_with_device.post("/session-claim", json={"session_id": "SID", "session_key": "SKEY"})
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    dev = client_with_device.device_id  # the id the helper authenticates as
    assert get_registry().resolve("SID") == dev
    assert get_registry().resolve("SKEY") == dev
```

> If `tests/test_plugin_api.py` has no reusable authed-client helper, model the test on the existing `/push-token` test (it must already authenticate a device session); replicate that setup. The route MUST reject an unauthenticated request with 403 via `_require_device_id` — add an assertion for that too if the existing tests show how to make an unauthed request.

- [ ] **Step 2: Run, verify fail** — `... pytest tests/test_plugin_api.py -q -k session_claim` → FAIL (404 route missing).
- [ ] **Step 3: Implement** — in `hermes_mobile/plugin_api.py`, add (near `PushTokenBody`/`set_push_token`):

```python
class SessionClaimBody(BaseModel):
    session_id: str
    session_key: str = ""


@router.post("/session-claim")
def claim_session(body: SessionClaimBody, request: Request) -> Dict[str, Any]:
    """Bind the calling device to a session so session-stop hooks can target it."""
    device_id = _require_device_id(request)
    from .session_notify import get_registry
    get_registry().claim(device_id, body.session_id.strip(), body.session_key.strip())
    return {"ok": True}
```

- [ ] **Step 4: Run, verify pass** — `... pytest tests/test_plugin_api.py -q` → PASS.
- [ ] **Step 5: Commit** — `git add hermes_mobile/plugin_api.py tests/test_plugin_api.py && git commit -m "feat(api): POST /session-claim binds device to session"`

---

## Task A5: Register hooks

**Files:** Modify `hermes_mobile/plugin.py`, `plugin.yaml`; Test `tests/test_plugin_registration.py`.

- [ ] **Step 1: Failing test** — create `tests/test_plugin_registration.py`. Reuse the existing `FakeCtx` from `tests/test_plugin_register.py` if present (it defines all `register_*` methods); otherwise define one with **all** methods `register_all` calls — confirmed: `register_dashboard_auth_provider`, `register_cli_command`, `register_platform`, plus the new `register_hook`:

```python
from hermes_mobile.device_store import DeviceStore
from hermes_mobile.plugin import register_all


class FakeCtx:
    def __init__(self):
        self.hooks = {}
    def register_dashboard_auth_provider(self, provider): pass
    def register_cli_command(self, *a, **k): pass
    def register_platform(self, **k): pass
    def register_hook(self, name, cb): self.hooks.setdefault(name, []).append(cb)


def test_register_all_registers_session_hooks(tmp_path):
    ctx = FakeCtx()
    register_all(ctx, store=DeviceStore(path=tmp_path / "devices.json"))
    assert "on_session_end" in ctx.hooks
    assert "pre_approval_request" in ctx.hooks
```

- [ ] **Step 2: Run, verify fail** — FAIL (hooks not registered).
- [ ] **Step 3: Implement** — in `register_all`, after platform registration, call `_register_session_notify(ctx, store)`; add helper:

```python
def _register_session_notify(ctx, store: DeviceStore) -> None:
    from .session_notify import SessionNotifier, get_registry
    notifier = SessionNotifier(store=store, registry=get_registry())
    ctx.register_hook("on_session_end", notifier.on_session_end)
    ctx.register_hook("pre_approval_request", notifier.on_pre_approval_request)
    logger.info("hermes-mobile: registered session-stop notifier hooks")
```

In `plugin.yaml` set `provides_hooks: [on_session_end, pre_approval_request]`.

- [ ] **Step 4: Run full suite** — `... pytest tests/ -q` → PASS.
- [ ] **Step 5: Commit** — `git add hermes_mobile/plugin.py plugin.yaml tests/test_plugin_registration.py && git commit -m "feat(plugin): register session-stop hooks"`

---

## Task A6: README + integration sanity

**Files:** Modify `README.md`.

- [ ] **Step 1: Document** under "Sending to the phone":

```markdown
### Session-stop notifications

When a run you started from the app stops — finished, asked a question, or
blocked on an approval — and you're not in the app, Hermes pushes a redacted
"come back" notification (also for finished cron runs). The device you're using
stays silent (the app suppresses the banner while foreground). The app binds its
device to each session via `POST /api/plugins/mobile/session-claim` so the
gateway knows where to push. Enabled by default; disable with
`MOBILE_NOTIFY_ON_SESSION_END=0`. Requires a gateway restart to load the hooks.
```

- [ ] **Step 2: Sanity check** —
`PYTHONPATH=$HOME/Developer/hermes-agent python -c "import os,sys; sys.path.insert(0, os.path.expanduser('~/.hermes/plugins/hermes-mobile')); from hermes_cli.plugins import discover_plugins, has_hook; discover_plugins(); print(has_hook('on_session_end'), has_hook('pre_approval_request'))"` → `True True`.
- [ ] **Step 3: Commit** — `git add README.md && git commit -m "docs: document session-stop notifications"`

---

# Part B — App (`~/Developer/hermes-mobile-app`, branch `feat/session-stop-push`)

> Create the branch: `git checkout main && git pull && git checkout -b feat/session-stop-push`. Commands from `~/Developer/hermes-mobile-app`.

## Task B1: `shouldSuppressForeground` pure function

**Files:** Modify `src/lib/push.ts`; Test `__tests__/push.test.ts`.

- [ ] **Step 1: Failing tests** — add to `__tests__/push.test.ts` (import the new symbol):

```ts
describe('shouldSuppressForeground', () => {
  it('suppresses session-stop pings while active', () => {
    expect(shouldSuppressForeground({ type: 'session_end' }, 'active')).toBe(true);
    expect(shouldSuppressForeground({ type: 'approval_request' }, 'active')).toBe(true);
  });
  it('shows when not active', () => {
    expect(shouldSuppressForeground({ type: 'session_end' }, 'background')).toBe(false);
    expect(shouldSuppressForeground({ type: 'approval_request' }, 'inactive')).toBe(false);
  });
  it('shows unknown/absent types even when active', () => {
    expect(shouldSuppressForeground({ type: 'other' }, 'active')).toBe(false);
    expect(shouldSuppressForeground(undefined, 'active')).toBe(false);
    expect(shouldSuppressForeground({}, 'active')).toBe(false);
  });
});
```

- [ ] **Step 2: Run, verify fail** — `npx jest push -t shouldSuppressForeground` → FAIL.
- [ ] **Step 3: Implement** — add to `src/lib/push.ts`:

```ts
export const SUPPRESSIBLE_PUSH_TYPES = ['session_end', 'approval_request'] as const;

/** Suppress the banner only for our session-stop pings while the app is the
 * active (foreground) app. Anything not clearly suppressible-while-active shows. */
export function shouldSuppressForeground(data: unknown, appState: string): boolean {
  if (appState !== 'active') return false;
  if (typeof data !== 'object' || data === null) return false;
  const type = (data as Record<string, unknown>).type;
  return typeof type === 'string' && (SUPPRESSIBLE_PUSH_TYPES as readonly string[]).includes(type);
}
```

- [ ] **Step 4: Run, verify pass** — `npx jest push && npx tsc --noEmit` → PASS, clean.
- [ ] **Step 5: Commit** — `git add src/lib/push.ts __tests__/push.test.ts && git commit -m "feat(push): shouldSuppressForeground for session-stop pings"`

---

## Task B2: Data-aware notification handler

**Files:** Modify `src/notifications.ts`. (Expo glue — verified by `tsc` + on-device.)

- [ ] **Step 1: Implement** — add `import { AppState } from 'react-native';` (merge into the existing import); add `shouldSuppressForeground` to the `@/lib/push` import; replace the `setNotificationHandler` body:

```ts
  Notifications.setNotificationHandler({
    handleNotification: async (notification) => {
      const data = notification.request.content.data;
      if (shouldSuppressForeground(data, AppState.currentState)) {
        return { shouldShowBanner: false, shouldShowList: false, shouldPlaySound: false, shouldSetBadge: false };
      }
      return { shouldShowBanner: true, shouldShowList: true, shouldPlaySound: false, shouldSetBadge: false };
    },
  });
```

- [ ] **Step 2: Typecheck + tests** — `npx tsc --noEmit && npx jest` → clean/pass.
- [ ] **Step 3: Commit** — `git add src/notifications.ts && git commit -m "feat(notifications): suppress session-stop banners while foreground"`

---

## Task B3: Claim the session after create/resume

**Files:** Modify `src/api/restClient.ts` (add `claimSession`); Modify `src/app/chat/[id].tsx` (call it).

- [ ] **Step 1: Add the REST helper** — in `src/api/restClient.ts`, add a method mirroring existing authenticated POSTs (it already posts to `/api/plugins/mobile/push-token` per the push flow — model on that):

```ts
async claimSession(sessionId: string, sessionKey: string): Promise<void> {
  await this.post('/api/plugins/mobile/session-claim', {
    session_id: sessionId,
    session_key: sessionKey,
  });
}
```

> Match the real `RestClient` POST signature/route-prefix used by the push-token call (grep `push-token` in `src/`). Use `PUSH_TOKEN_ROUTE`'s sibling style; add `SESSION_CLAIM_ROUTE = '/api/plugins/mobile/session-claim'` to `src/lib/push.ts` if route constants live there.

- [ ] **Step 2: Call it after create/resume** — in `src/app/chat/[id].tsx`, after `session.create` (where `created.session_id` and `created.stored_session_id` are set, ~line 485) and after `session.resume` (where `resumed.session_id` is set, ~line 318), fire-and-forget a claim. Use the live id + the stored id (which is the session_key):

```ts
// after create: liveIdRef.current = created.session_id; storedIdRef.current = created.stored_session_id
void withAuthRetry((r) => r.claimSession(liveIdRef.current!, storedIdRef.current ?? liveIdRef.current!)).catch(() => {});
// after resume: liveIdRef.current = resumed.session_id (storedIdRef already set)
void withAuthRetry((r) => r.claimSession(liveIdRef.current!, storedIdRef.current ?? liveIdRef.current!)).catch(() => {});
```

> Best-effort: never block the chat flow on the claim. `withAuthRetry` is the existing wrapper used for authenticated REST calls (grep its usage in `[id].tsx`).

- [ ] **Step 3: Typecheck + tests** — `npx tsc --noEmit && npx jest` → clean/pass.
- [ ] **Step 4: Commit** — `git add src/api/restClient.ts src/lib/push.ts src/app/chat/[id].tsx && git commit -m "feat(chat): claim session with device for session-stop push"`

---

# Finalize
- [ ] **Plugin:** `PYTHONPATH=$HOME/Developer/hermes-agent python -m pytest tests/ -q` green; push `feat/session-stop-push`; open PR "feat: session-stop push notifications" (summarize triggers, claim-map attribution, targeting, suppression; link the spec; note gateway restart + `MOBILE_NOTIFY_ON_SESSION_END`).
- [ ] **App:** `npx tsc --noEmit && npx jest` green; push `feat/session-stop-push`; open PR "feat: session-stop push (claim + foreground suppression)" referencing the plugin PR.
