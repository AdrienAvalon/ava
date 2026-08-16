# Telemetry

The Ava fork keeps external analytics **disabled by default**.  A user may
explicitly opt in to the upstream anonymous telemetry described below; local
performance telemetry remains a separate, non-exported subsystem.

## TL;DR

- **Off by default in the Ava fork**; explicit opt-in only.
- **Anonymous** — one random UUID per install, no email, no name, no IP.
- **No chat content, ever.** Only counts, timings, and feature names.
- **Self-hosted backend** on the OpenJarvis team's PostHog instance —
  data is not sold or shared with third parties.
- **365-day retention**, after which events are deleted automatically.

## What we collect

### Lifecycle events

| Event | Source | Why we send it |
|---|---|---|
| `install_started` | `install.sh` | Top of install funnel |
| `install_stage_completed` | `install.sh` | Per-stage timing — where do people drop off? |
| `install_completed` | `install.sh` | Did the install succeed? |
| `install_failed` | `install.sh` | Which stage failed, and on what OS |
| `app_opened` | Backend + frontend | DAU / WAU / MAU |
| `setup_completed` | Frontend | First-run wizard finished |
| `first_chat_sent` | Backend | First-ever message — activation |
| `uninstall_started` | `uninstall.sh` (if user runs it) | Churn signal |

### Usage events

| Event | Why we send it |
|---|---|
| `chat_session_ended` | Aggregated per-session: turn count, tokens, latency, tool count |
| `tool_first_used` | Which built-in tools are actually adopted |
| `model_changed` | How often users switch models |
| `feature_used` | Which features get traffic, which don't |
| `connector_auth_completed` | Which connectors people set up |
| `error_shown_to_user` | User-visible error class (not stack trace) |
| `feedback_submitted` | Was a rating given? Was a comment included? |
| `settings_changed` | Which settings get toggled |
| `usage_daily_summary` | Once-per-day aggregated counts |

The canonical, authoritative list with every property name and its
type validator lives in
[`src/openjarvis/analytics/events.py`](../src/openjarvis/analytics/events.py).
That file is the only place new events can be added — PR review is
the gate.

## What we never collect

Hard guardrails, enforced by code:

- **Chat content** — prompts, model outputs, system messages, tool args.
- **File paths** — anything matching `~/`, `$HOME`, `/Users/<name>`, `/home/<name>`, `file://`.
- **Emails, names, phone numbers, addresses.**
- **IP addresses** (IPv4 + IPv6). PostHog's IP geo lookup is disabled server-side too.
- **MAC addresses, hardware serials, drive UUIDs.**
- **Stack traces** — only error class enums.
- **API keys, OAuth tokens, JWTs, bearer tokens, password assignments** —
  matched and dropped at value level.
- **Hostnames** that look personal (e.g. `alice-macbook.local`).
- **Lists, dicts, sets** — composite values are never sent so PII can't
  smuggle through inside containers.

Two independent filters run before every event leaves the machine:

1. [`src/openjarvis/analytics/redaction.py`](../src/openjarvis/analytics/redaction.py) — value-level pattern matching (20+ regexes for PII).
2. [`src/openjarvis/analytics/events.py`](../src/openjarvis/analytics/events.py) — structural allowlist (event name + property name + type validator).

Any failure at either layer → the event or property is silently
dropped. Tests covering the patterns: [`tests/analytics/test_redaction.py`](../tests/analytics/test_redaction.py).

## Where the data goes

- **Today** (alpha): PostHog Cloud (US region) free tier. Disclosed
  here for transparency.
- **Production target**: A self-hosted PostHog instance at
  `analytics.openjarvis.ai`, Hetzner US-East. Single-tenant, operated
  by the OpenJarvis team.
- **Never** sold, shared with advertisers, or used for anything other
  than improving OpenJarvis.

## Retention

- Default retention: **365 days**, then events are deleted by PostHog
  automatically.
- `jarvis analytics reset-id` lets you orphan all of your past events
  by generating a fresh anonymous ID for future events.

## How identity works

A single UUID v4 is generated on first install and stored at
`~/.openjarvis/anon_id`. The install script, backend, and frontend all
read the same file so events across the full lifecycle tie to one
person — without us ever knowing who that person is.

## Opting in or out

The Ava fork disables external analytics by default. Runtime analytics require
`[analytics] enabled = true`; the installer additionally requires
`OPENJARVIS_ENABLE_ANALYTICS=1`.  Either process-level kill switch still wins:

```bash
export DO_NOT_TRACK=1
# or
export OPENJARVIS_NO_ANALYTICS=1
```

Any value other than an empty string, `0`, `false`, `no`, or `off` disables external
analytics. The environment always takes precedence over the TOML setting. Avalon sets
`OPENJARVIS_NO_ANALYTICS=1` declaratively in addition to keeping the TOML setting false.

Deleting `~/.openjarvis/anon_id` only breaks the link with the previous anonymous ID;
it does not itself disable future events.

## For researchers and contributors

- **Adding an event**: edit `src/openjarvis/analytics/events.py`,
  declare the spec, then update this page. PR review enforces both.
- **Adding a PII pattern**: edit `src/openjarvis/analytics/redaction.py`
  and add a test case in `tests/analytics/test_redaction.py`.
- **Inspecting what your install sends**: run with
  `OPENJARVIS_LOG_LEVEL=DEBUG` and grep for `Analytics`. You'll see
  every event name and (redacted) property dict before it ships.

## Related

- Local telemetry (FLOPs, energy, latency stored in
  `~/.openjarvis/telemetry.db`) is a **separate** subsystem documented
  in [`src/openjarvis/telemetry/`](../src/openjarvis/telemetry/). It
  never leaves the machine and is controlled by `[telemetry]` (not
  `[analytics]`) in `config.toml`.
- The leaderboard / contest opt-in (`OptInModal.tsx`) is a separate,
  voluntary feature that publicly shares your energy and savings on
  the OpenJarvis leaderboard. It is **not** the same as analytics and
  requires explicit opt-in with a display name and email.
