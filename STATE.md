# State

_Last updated: 2026-09-29_

## Supported cores
The plugin is tested against the two hermes-agent tags that dc1-1 can run:
- v2026.8.18 (0.20.4), the rollback image
- v2026.9.24 (0.21.5), live

The full suite must pass on both (see README → Development).

## CI
Done in #11 (review F7). `.github/workflows/ci.yml` runs on every PR and push to
`main`:
- `pytest (hermes v2026.8.18)` and `pytest (hermes v2026.9.24)`: the upstream
  core at its pinned tag commit, with the core's pyproject dependencies, on
  Python 3.13.
- `ruff`: `check` and `format --check`.
Branch protection is not set, so the checks are not yet required to merge.

## Current release: 0.2.1 (main)
`plugin.yaml` and `dashboard/manifest.json` are both at 0.2.1.
- `mobile:<device_id>` resolves on both cores through `parse_target_ref_fn` and
  `validate_target_ref_fn`. `hermes send -t mobile:<id>` delivers out of process
  through `standalone_sender_fn`.
- The fields are feature-detected (`init` fields only). If `register_platform`
  still raises `TypeError` with them, the plugin retries once without them, so the
  `mobile` platform always loads.
- Push tokens are redacted from logged Expo errors (Expo-shaped tokens in any
  case, escaped or truncated, and the device's own token, truncated too).

## Delivery surfaces (0.21.5)
- Agent to phone goes through cron only (the `cronjob` tool with
  `deliver=mobile[:<id>]`). Upstream removed the agent-callable `send_message`
  tool.
- `MOBILE_HOME_CHANNEL` sets the default device for bare `deliver=mobile`.
- `hermes send -t mobile:<id>` runs from a shell as the gateway user.

## Known gaps / follow-ups
- `hermes send -t mobile` (bare, no id) reads the gateway config's
  `platforms.mobile.home_channel`, not `MOBILE_HOME_CHANNEL`. Pass the id
  explicitly.
- **Mailbox drain lock (review F6).** `drain_messages` does `read_text()` then
  `unlink()`, and `append_message` takes no lock. An append that lands between
  the read and the unlink is lost, and so is one whose fd was opened before the
  unlink (it goes into an orphaned inode). The sender still reports success.
  This was already true for the live adapter, and now `hermes send` is one more
  writer. Fix: an `fcntl.flock(LOCK_EX)` on a `mailbox/<id>.lock` sidecar in both
  functions, held by the drain from read through truncate/unlink (the same
  pattern DeviceStore uses).

## Deploy note
The box's boot pulls plugin `main`. After a merge, the next gateway restart runs
the new code.
