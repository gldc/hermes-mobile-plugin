# Changelog

All notable changes to hermes-mobile-plugin. Versions match `plugin.yaml`.

## 0.2.1 — 2026-09-29

### Fixed
- `hermes send -t mobile:<device_id>` works on hermes 0.21.5. The core resolves
  plugin targets only through `PlatformEntry.parse_target_ref_fn`, and a 16-hex
  device id matched none of its heuristics, so the command failed with "Could not
  resolve '<id>' on mobile". The plugin now registers:
  - `parse_target_ref_fn`: a device id (16 hex chars; case and whitespace are
    normalised) returns `(device_id, None)`. Anything else returns `None`. Names
    are never resolved, because they are not unique.
  - `validate_target_ref_fn`: a paired, unrevoked device returns `True`. Other ids
    get a diagnostic: `device <id> is revoked`, or
    ``no paired device <id> — see `hermes mobile devices` ``. It re-reads
    `devices.json` on every call, because the CLI runs in another process.
  - `standalone_sender_fn`: out-of-process delivery for `hermes send`, `hermes cron
    run`, and cron's standalone fallback. It runs `MobileAdapter.send` itself (the
    same mailbox append and redacted push). As root, it refuses to write a mailbox
    tree owned by another uid (`HERMES_DOCKER_EXEC_AS_ROOT=1`).
- Each field is passed only when this core's `PlatformEntry` declares it as a
  constructor field (`init=True`), because `register_platform` raises `TypeError`
  on unknown kwargs. v2026.8.18 (0.20.4) and v2026.9.24 (0.21.5) both declare all
  three. If `register_platform` still raises `TypeError` with them, the plugin logs
  a warning and registers once more without them, so the `mobile` platform always
  loads.
- Push tokens are redacted from logged Expo errors. The network error, the HTTP
  error body and the ticket error all mask the device's token and any
  `ExponentPushToken[...]`/`ExpoPushToken[...]` before they are logged. Expo's
  `DeviceNotRegistered` message echoes the token, and these warnings can now come
  from `hermes send` as well as from the gateway.

### Changed
- Cron `deliver=mobile:<id>` is now strict. An id that is unknown or revoked is
  rejected when the job resolves its target, and a warning is logged. Before this
  release, the message went to a mailbox that no device drains. Bare
  `deliver=mobile` (`MOBILE_HOME_CHANNEL`) does not change.
- Docs: hermes 0.21 removed the agent-callable `send_message` tool. Agent-to-phone
  delivery is now cron-only (the `cronjob` tool with `deliver=mobile[:<id>]`), and
  `MOBILE_HOME_CHANNEL` sets the default device.

## 0.2.0 — 2026-09-29

- `DeviceStore` locking: a process-wide lock plus a `flock` on
  `devices.json.lock`, with fd-based root-write ownership hand-back.
- Coalesced approval prompts push once. `clarify` pushes "Hermes has a question"
  through the `pre_tool_call` hook.

## 0.1.0 — 2026-06-11

- First release. It adds the `mobile-device` dashboard auth provider, the
  `hermes mobile` CLI, the `mobile` platform adapter (mailbox and redacted Expo
  push) and the dashboard API. The memory API and session-stop notifications
  shipped later under the same version.
