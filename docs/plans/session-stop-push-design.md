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

Three hermes-agent hooks, all observers:

| Hook | Fires when | Push |
|---|---|---|
| `on_session_end` (`agent/turn_finalizer.py`) | end of every `run_conversation` — per turn, immediately; kwargs `session_id, task_id, turn_id, completed, interrupted, model, platform` | "Your session is ready — tap to check" (`data.type=session_end`) |
| `pre_approval_request` (`tools/approval.py`) | agent blocks on a dangerous command; kwargs `command, description, pattern_key(s), session_key, surface` | "Hermes needs your approval" (`data.type=approval_request`) |
| cron → `on_session_end` | the cron run's `run_conversation` ends | same as `session_end` |

`on_session_end` is the **plugin hook** fired in `turn_finalizer` (per run) — not
the `ContextEngine.on_session_end()` method, which is a different thing that
fires only at hard session boundaries.

---

## 3. Qualification rules

A trigger pushes only if ALL hold:

1. **Feature enabled** — `MOBILE_NOTIFY_ON_SESSION_END`, **default on**.
2. **Qualifying session:**
   - **Mobile-originated** — the session's `source.user_id` starts with
     `mobile:` (the `mobile-device` auth provider's identity). Resolved from the
     `session_store` reference / a `session_id → device_id` map the plugin builds
     in `pre_gateway_dispatch`.
   - **OR autonomous cron** — `HERMES_CRON_SESSION=1` / `platform == "cron"`.
   - Else **skip** — browser dashboard, interactive CLI, and subagent/background
     runs carry neither a `mobile:` identity nor the cron marker, so they are
     excluded for free.
3. **Not user-interrupted** — `on_session_end` with `interrupted=True` is skipped
   (you interrupted it → you are present).
4. **Not already delivered to mobile** — if the run already pushed via
   `deliver=mobile` / a `send_message` to a mobile target, skip the stop-ping (no
   double notification).
5. **Approval surface == "gateway"** for `pre_approval_request` (CLI-surface
   approvals are someone at a terminal).

---

## 4. Targeting

Fan out to **all non-revoked devices that have a push token**
(`DeviceStore.list_devices()` filtered). Per the product decision; foreground
suppression (§6) keeps the device you are using quiet. Stale test devices will
also receive — a `hermes mobile revoke` cleanup is recommended but out of scope.

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
  - `SessionNotifier(store, push, config)` — session→device associations,
    qualification, fan-out.
  - `on_pre_gateway_dispatch(event, gateway, session_store)` — record
    `{session_id → device_id}` for mobile-originated events; capture the
    `session_store` reference.
  - `on_session_end(**kwargs)` — qualify + fan-out (`session_end`).
  - `on_pre_approval_request(**kwargs)` — qualify + fan-out (`approval_request`).
- **`hermes_mobile/plugin.py` `register_all`:** `ctx.register_hook(...)` for the
  three hooks.
- **`hermes_mobile/push.py`:** `ExpoPush.send` gains an optional `data` dict.
- **`plugin.yaml`:** `provides_hooks: [pre_gateway_dispatch, on_session_end,
  pre_approval_request]`.
- Reuses `DeviceStore` and `ExpoPush`.

### App (`hermes-mobile-app`)
- `src/notifications.ts` — data-aware `setupNotificationHandling` + tap routing.
- `src/lib/push.ts` — `shouldSuppressForeground(data, appState)` pure fn + `data`
  parsing.

---

## 8. Data flow

```
app submits prompt (mobile-device session) ──► gateway
   │ pre_gateway_dispatch: plugin records session_id → device_id
   ▼
agent runs (maybe long; maybe hits an approval)
   ├─ approval needed ─► pre_approval_request ─► qualify(mobile) ─► push data.type=approval_request → all devices
   ▼
run_conversation ends ─► on_session_end ─► qualify(mobile/cron, not interrupted, not already-mobile-delivered)
   ▼
ExpoPush.send(token, body, data) → Expo → APNs → each device
   ├─ app foreground  → suppressed (no banner)
   └─ app backgrounded → "Your session is ready" banner
```

---

## 9. Edge cases / noise control
- **Multi-turn in foreground:** each turn ends → push fires → suppressed (you are
  in the app). No spam.
- **Subagent / background-review runs:** not in the session→device map (not
  user-dispatched mobile prompts) → excluded.
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
- **Plugin (TDD, pytest):** qualification matrix — mobile vs browser
  (`api_server`, non-`mobile:` user_id) vs cron vs interactive-CLI vs subagent;
  `interrupted` skip; already-delivered-to-mobile dedup; approval surface filter;
  fan-out to all non-revoked tokened devices; redaction (no content in payload);
  `data.type` correctness; feature toggle off → no push.
- **App (jest):** `shouldSuppressForeground` (active + known type → suppress;
  background → show; unknown type → show); `data.type` parsing.
- **On-device:** background app + run a task → banner; foreground → silent;
  approval while away → banner → open → respond.

---

## 12. Plan-time verification items (load-bearing — confirm before/while building)
1. Exact `session_store` API to resolve `session_id` / `session_key` →
   `source.user_id` (iterate `_entries` vs a maintained map). Confirm
   `session_key` (approval) and `session_id` (session_end) both correlate to the
   same entry/device.
2. Confirm `pre_approval_request` fires for the mobile/dashboard WS approval path
   (`surface == "gateway"`) and lines up with the app's existing
   `approval.request` WS event.
3. Confirm `on_session_end`'s `platform`/markers for a mobile WS session and for
   cron (`HERMES_CRON_SESSION`) to pick the cheapest qualification check.
4. Confirm the dedup signal for "already delivered to mobile" (e.g.
   `HERMES_CRON_AUTO_DELIVER_PLATFORM == "mobile"`, or counting adapter sends
   within the run).

---

## 13. File changes summary

| Repo / File | Change |
|---|---|
| plugin `hermes_mobile/session_notify.py` | NEW — notifier + 3 hook callbacks |
| plugin `hermes_mobile/plugin.py` | register the 3 hooks in `register_all` |
| plugin `hermes_mobile/push.py` | `ExpoPush.send` accepts optional `data` dict |
| plugin `plugin.yaml` | `provides_hooks` lists the 3 hooks |
| plugin `tests/test_session_notify.py` | NEW — qualification / fan-out / redaction tests |
| plugin `README.md` | document the feature + toggle |
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
