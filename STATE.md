# State

_Last updated: 2026-09-29_

## Supported cores
The plugin is tested against the two hermes-agent tags that dc1-1 can run:
- v2026.8.18 (0.20.4), the rollback image
- v2026.9.24 (0.21.5), live

The full suite must pass on both (see README → Development).

## Current release: 0.2.1 (branch `fix/mobile-target-parser-0215`, not merged)
- `mobile:<device_id>` resolves on both cores through `parse_target_ref_fn` and
  `validate_target_ref_fn`. `hermes send -t mobile:<id>` delivers out of process
  through `standalone_sender_fn`.
- The fields are feature-detected, so an older `PlatformEntry` without them still
  registers.

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
- Mailbox drain (`GET /mailbox`) reads the file and then unlinks it. An append
  that lands between the read and the unlink is lost. This was already true for
  the live adapter, and now `hermes send` is one more writer.

## Deploy note
The box's boot pulls plugin `main`. After a merge, the next gateway restart runs
the new code.
