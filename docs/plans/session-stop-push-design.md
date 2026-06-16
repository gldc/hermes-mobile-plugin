# Session-Stop Push Notifications — Design

**Date:** 2026-06-16
**Status:** Draft
**Owner:** [@gldc](https://github.com/gldc)
**Repos:** `hermes-mobile-plugin` (server-side triggers), `hermes-mobile-app` (foreground suppression)
**Plugin branch:** `feat/session-stop-push` · **App branch:** TBD

---

## 1. Overview / Motivation

Today the only way the agent reaches the phone is an explicit `send_message`
or cron `deliver=mobile` to the `mobile` platform (outbound mailbox + redacted
push). Nothing fires when an agent run you started **from the app** stops. The
driving use case:

> You open the app, ask Hermes to do something potentially long, and leave the
> app. You want a push when the session stops — so you can come back and see
> whether it finished or needs your input.

This adds **automatic "come back" pushes** driven by hermes-agent lifecycle
hooks, scoped to mobile-originated sessions (plus cron/autonomous runs), and
suppressed on whatever device you are actively using.

### 1.1 Goals
- Push when a mobile-originated run stops (finished, or ended its turn awaiting
  your reply).
- Push when the agent blocks on an **approval** in a mobile session (the app can
  already respond to approvals, so this is actionable).
- Push when an autonomous **cron** run finishes.
- Never fire on a device where the app is currently foreground.
- **Zero hermes-agent fork** — use only documented plugin hooks (the plugin's
  core promise).

### 1.2 Non-goals (v1)
- Notifying for non-mobile interactive sessions (browser dashboard, interactive
  CLI).
- Generic non-interactive CLI / gateway-proactive runs (need verified markers;
  later).
- Per-message / per-tool progress pings (only run-stop + approval).
- Message content in the push (stays redacted; `data` carries routing only).

---

## 2. Triggers (server-side, in the plugin)

Two hermes-agent hooks, both observers, both **verified to fire on the app's
`/api/ws` → tui_gateway path** (the agent runs via `run_conversation` at
`tui_gateway/server.py:5881`):

| Hook | Fires when | kwargs (verified) | Push |
|---|---|---|---|
| `on_session_end` (`agent/turn_finalizer.py:415`) | end of every `run_conversation` — per turn, immediately | `session_id` (live), `task_id` (= tui `session_key`), `turn_id`, `completed`, `interrupted`, `model`, `platform` (`"tui"` for app runs) | "Your session is ready — tap to check" (`data.type=session_end`) |
| `pre_approval_request` (`tools/approval.py:1267`) | agent blocks on a dangerous command (gateway approval flow) | `command`, `description`, `pattern_key(s)`, `session_key`, `surface` (`"gateway"` for app runs) | "Hermes needs your approval" (`data.type=approval_request`) |

`on_session_end` is the **plugin hook** fired in `turn_finalizer` (per run) — not
the `ContextEngine.on_session_end()` method (a different thing). `cron` runs also
fire `on_session_end` (with `HERMES_CRON_SESSION=1` in the environment).

### 2.1 Device attribution — the session-claim map

The blocker found in review: on the app path `platform == "tui"` and there is
**no `mobile:<device_id>` identity** on the session (`pre_gateway_dispatch` does
not fire here, and `SessionStore.origin.user_id` is unset for tui sessions). The
hooks carry only server-generated ids (`session_id`, `session_key`). So the
plugin learns the device→session link itself, at session-create time, via a
**plugin-owned authenticated route**:

- The app, right after `session.create`/`session.resume`, calls
  `POST /api/plugins/mobile/session-claim` with
  `{session_id, session_key}` — where `session_id` is the live id it already
  holds (`liveIdRef`) and `session_key` is `stored_session_id` (`storedIdRef`),
  which **equals the tui `session_key`** the hooks carry
  (`tui_gateway/server.py:3949-3950` returns `{"session_id": sid,
  "stored_session_id": key}`).
- The request is authenticated by the `mobile-device` provider, so the plugin
  reads the device id via the existing `_require_device_id(request)`
  (`plugin_api.py:108`) — no identity plumbing, no fork.
- The plugin records `{session_id → device_id, session_key → device_id}` in an
  in-process TTL map (`SessionClaimRegistry`). `on_session_end` resolves by
  `session_id` (fallback `task_id`); `pre_approval_request` resolves by
  `session_key`. Both hit from the first turn.

This works because the tui_gateway WS agent run and the `/api/plugins/mobile/*`
routes execute in the **same dashboard web-server process**, so the in-process
map is shared (see §12.1).

---

## 3. Qualification rules

A trigger pushes only if ALL hold:

1. **Feature enabled** — `MOBILE_NOTIFY_ON_SESSION_END`, **default on**.
2. **Qualifying session:**
   - **Mobile-originated** — the hook's id is in the `SessionClaimRegistry`
     (§2.1): `on_session_end` looks up `session_id` (fallback `task_id`);
     `pre_approval_request` looks up `session_key`. A hit yields the `device_id`.
   - **OR autonomous cron** — `HERMES_CRON_SESSION == "1"` (set in the
     environment by the scheduler).
   - Else **skip** — browser dashboard, interactive CLI, and subagent/background
     runs never claimed a device, so they miss the registry and are excluded.
3. **Not user-interrupted** — `on_session_end` with `interrupted=True` is skipped
   (you interrupted it → you are present).
4. **Not already delivered to mobile** (cron only) — if the cron run targets a
   mobile delivery, skip the stop-ping (no double notification). Read via
   `gateway.session_context.get_session_env("HERMES_CRON_AUTO_DELIVER_PLATFORM")`
   — it is a **ContextVar, not an env var**, so `os.getenv` would silently
   return nothing.
5. **Approval surface == "gateway"** for `pre_approval_request` (CLI-surface
   approvals are someone at a terminal; the app path is always `"gateway"`).

> **Subagent guard:** `on_session_end` fires for every `run_conversation`,
> including subagent/background-review runs. Those never claim a device, so the
> registry miss already excludes them — but if a future change gave subagents a
> claimed session, also gate on the run being a top-level user turn. For v1 the
> registry miss is the guard.

---

## 4. Targeting

Fan out to **only non-revoked devices that have a registered push token**
(`DeviceStore.list_devices()` → keep where `revoked is False and push_token`).
Devices without a token are skipped (app never paired, or push never
registered) — no wasted Expo calls. Foreground suppression (§6) keeps the
device you are actively using quiet. Note: a stale *tokened* test device still
receives; periodic `hermes mobile revoke` hygiene is recommended (out of scope
for v1).

---

## 5. Push payload

Redacted, consistent with the existing convention, **plus a `data` field**
(`push.py` sends none today):

```json
{"to": "<token>", "title": "Hermes",
 "body": "Your session is ready — tap to check",
 "data": {"type": "session_end"}}
```

- `data.type` ∈ {`session_end`, `approval_request`}.
- Message content never rides the push (redacted). `data` carries routing only
  (type now; a session/device id later for deep-linking).
- Requires extending `ExpoPush.send` to accept an optional `data` dict (currently
  title/body only).

---

## 6. Foreground suppression (app-side — Option 1)

The push is delivered to all devices; each device decides whether to show it
based on its own foreground state:

- `setupNotificationHandling` becomes **data-aware**: for a notification whose
  `data.type` ∈ {`session_end`, `approval_request`}, return
  `shouldShowBanner: false` (no list/sound) while `AppState.currentState ===
  'active'`. Backgrounded → show normally.
- The decision is a **pure function** `shouldSuppressForeground(data, appState)`
  in `src/lib` → unit-tested.
- Tap routing: both types → chat home (where a pending `ApprovalCard` lives).
  Deep-link to a specific session is a follow-up once `data` carries a session id.

---

## 7. Architecture / components

### Plugin (`hermes-mobile-plugin`)
- **NEW `hermes_mobile/session_notify.py`:**
  - `SessionClaimRegistry` — process-wide, thread-safe, TTL map
    `id → device_id` (keyed by both `session_id` and `session_key`); a single
    module-level instance shared by the route and the notifier.
  - `SessionNotifier(store, push, registry)`:
    - `on_session_end(session_id, task_id, interrupted, **_)` — qualify
      (registry hit on `session_id`/`task_id`, or `HERMES_CRON_SESSION`) +
      fan-out (`session_end`).
    - `on_pre_approval_request(session_key, surface, **_)` — qualify (registry
      hit on `session_key`, `surface == "gateway"`) + fan-out (`approval_request`).
  - (No `pre_gateway_dispatch` — it does not fire on the app path.)
- **`hermes_mobile/plugin_api.py`:** NEW `POST /session-claim` route — reads
  `device_id` via the existing `_require_device_id(request)`, records
  `{session_id, session_key} → device_id` in the shared `SessionClaimRegistry`.
- **`hermes_mobile/plugin.py` `register_all`:** `ctx.register_hook("on_session_end", …)`
  and `ctx.register_hook("pre_approval_request", …)`, wiring the same registry
  instance the route uses.
- **`hermes_mobile/push.py`:** `ExpoPush.send` gains an optional `data` dict.
- **`plugin.yaml`:** `provides_hooks: [on_session_end, pre_approval_request]`.
- Reuses `DeviceStore`, `ExpoPush`, and the existing `APIRouter` + `_require_device_id`.

### App (`hermes-mobile-app`)
- `src/app/chat/[id].tsx` — after `session.create`/`session.resume`, fire-and-forget
  `POST /api/plugins/mobile/session-claim {session_id: liveId, session_key: storedId}`
  (re-claim on resume, since the live id changes).
- `src/api/restClient.ts` (or wherever REST calls live) — a `claimSession` helper.
- `src/notifications.ts` — data-aware `setupNotificationHandling` + tap routing.
- `src/lib/push.ts` — `shouldSuppressForeground(data, appState)` pure fn + `data`
  parsing.

---

## 8. Data flow

```
app: session.create/resume → {session_id, stored_session_id(=session_key)}
   │ app → POST /api/plugins/mobile/session-claim {session_id, session_key}
   │ plugin (authenticated) → _require_device_id → registry[session_id]=registry[session_key]=device_id
   ▼
app submits prompt over /api/ws (tui_gateway) ──► agent.run_conversation(...)
   ├─ approval needed ─► pre_approval_request(session_key, surface="gateway")
   │      └─ registry[session_key] hit → fan-out push data.type=approval_request
   ▼
run_conversation ends ─► on_session_end(session_id, task_id, interrupted)
   └─ registry[session_id|task_id] hit (or HERMES_CRON_SESSION) & not interrupted
        → fan-out: ExpoPush.send(token, body, data={"type":"session_end"}) → Expo → APNs → each tokened device
             ├─ app foreground  → suppressed (no banner)
             └─ app backgrounded → "Your session is ready" banner
```

---

## 9. Edge cases / noise control
- **Multi-turn in foreground:** each turn ends → push fires → suppressed (you are
  in the app). No spam.
- **Subagent / background-review runs:** never claimed a device → miss the
  registry → excluded.
- **Process locality:** the registry is in-process. The app's tui_gateway WS
  agent run and the `/api/plugins/mobile/*` routes share the dashboard
  web-server process, so a claim made by the route is visible to the hook (§12.1).
  Cron runs in that same gateway process and qualifies via `HERMES_CRON_SESSION`,
  not the registry.
- **Approval timeout while away:** push already sent; if it times out before you
  respond, reopening reflects current state. Acceptable.
- **Race:** run ends just as you background the app → push arrives backgrounded →
  shown (desired).
- **Multiple devices:** all backgrounded devices buzz; the active one is silent
  (per the "all devices" decision).

---

## 10. Configuration
- `MOBILE_NOTIFY_ON_SESSION_END` (env or `config.yaml`) — master toggle, default
  **on**.
- Future: per-type toggles, quiet hours, per-device opt-out.

---

## 11. Testing
- **Plugin (TDD, pytest):**
  - `SessionClaimRegistry`: claim then resolve by `session_id` and by
    `session_key`; unknown id → miss; TTL eviction; thread-safety smoke.
  - `SessionNotifier` qualification matrix: claimed session → push; unclaimed
    (browser/CLI/subagent) → no push; cron (`HERMES_CRON_SESSION=1`) → push;
    cron + mobile delivery (via a patched `get_session_env`) → dedup/no push;
    `interrupted=True` → no push; approval `surface != "gateway"` → no push;
    fan-out to non-revoked tokened devices only; redaction (no content in
    payload); `data.type` correctness; toggle off → no push.
  - `session-claim` route: authenticated request records both ids → device;
    unauthenticated/no-device → 403 (existing `_require_device_id` behavior).
- **App (jest):** `shouldSuppressForeground` (active + known type → suppress;
  background → show; unknown type → show); `data.type` parsing.
- **On-device:** background app + run a task → banner; foreground → silent;
  approval while away → banner → open → respond.

---

## 12. Verification status (post-review)

**Resolved (verified against source):**
- `on_session_end` and `pre_approval_request` both fire on the app's tui_gateway
  WS path (`tui_gateway/server.py:5881` → `run_conversation` → `turn_finalizer.py:415`;
  approvals via `tools/approval.py:1267`, `surface="gateway"`). `pre_gateway_dispatch`
  does **not** fire here — dropped from the design.
- `session.create` returns `{"session_id": sid, "stored_session_id": key}` where
  `key` is the tui `session_key` (`tui_gateway/server.py:3949-3950`); the app holds
  both (`liveIdRef`, `storedIdRef`). `on_session_end` carries `session_id` + `task_id`
  (= session_key); `pre_approval_request` carries `session_key`. So a claim of
  `{session_id, session_key}` resolves both hooks from turn 1.
- Cron dedup var `HERMES_CRON_AUTO_DELIVER_PLATFORM` is a ContextVar
  (`gateway/session_context.py:66`) → must read via `get_session_env`, not
  `os.getenv`. `HERMES_CRON_SESSION` is a real env var (`os.getenv` ok).
- Device identity reachable in any `/api/plugins/mobile/*` request via
  `_require_device_id` (`plugin_api.py:108`).

**Remaining (confirm during implementation):**
1. **Process locality (load-bearing):** confirm the tui_gateway WS agent run and
   the `/api/plugins/mobile/*` routes execute in the **same process**, so the
   in-process `SessionClaimRegistry` is shared. (Strongly expected — the dashboard
   web server hosts both — but verify; if a deployment splits them, the registry
   must move to a shared store, e.g. a small file/SQLite, keyed the same way.)
2. Confirm the app's `session.create`/`session.resume` result fields are exactly
   `session_id` and `stored_session_id` as consumed in `src/app/chat/[id].tsx`.

---

## 13. File changes summary

| Repo / File | Change |
|---|---|
| plugin `hermes_mobile/session_notify.py` | NEW — `SessionClaimRegistry` + `SessionNotifier` (2 hook callbacks) |
| plugin `hermes_mobile/plugin_api.py` | NEW `POST /session-claim` route (uses `_require_device_id` + shared registry) |
| plugin `hermes_mobile/plugin.py` | register `on_session_end` + `pre_approval_request` hooks in `register_all`, wiring the shared registry |
| plugin `hermes_mobile/push.py` | `ExpoPush.send` accepts optional `data` dict |
| plugin `plugin.yaml` | `provides_hooks: [on_session_end, pre_approval_request]` |
| plugin `tests/test_session_notify.py` | NEW — registry + qualification/fan-out/redaction tests |
| plugin `tests/test_plugin_api.py` | session-claim route tests (extend existing) |
| plugin `README.md` | document the feature + toggle |
| app `src/api/restClient.ts` | `claimSession(session_id, session_key)` helper |
| app `src/app/chat/[id].tsx` | claim after `session.create`/`session.resume` |
| app `src/notifications.ts` | data-aware foreground suppression + tap routing |
| app `src/lib/push.ts` | `shouldSuppressForeground` pure fn + `data` parsing |
| app `__tests__/push.test.ts` | suppression-decision tests |

---

## 14. Relationship to existing work
- Complements `cron_deliver_env_var` (branch `feat/cron-delivery-target`): that
  lets cron *deliver results* to mobile; this pings when a run *stops*. The dedup
  rule (§3.4) prevents both firing for the same cron run.
- Builds on the redacted-push + `DeviceStore` + `ExpoPush` surfaces already in
  the plugin, and the app's existing `setupNotificationHandling` + `ApprovalCard`.
