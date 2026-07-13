[中文](README.md) | **English**

# Codex Usage for Home Assistant

A minimal Home Assistant custom integration that tracks OpenAI Codex
(ChatGPT subscription) rate-limit usage.

It polls the same undocumented endpoint the Codex client uses
(`https://chatgpt.com/backend-api/wham/usage`) and provides these sensors:

| Sensor | Description |
| --- | --- |
| 5-Hour Usage | Percentage used of the rolling 5-hour window |
| 5-Hour Reset Time | When the 5-hour window resets |
| Weekly Usage | Percentage used of the weekly window |
| Weekly Reset Time | When the weekly window resets |
| Plan Type | ChatGPT/Codex plan type (e.g. `plus`, `pro`) |
| Credits Balance | Pay-as-you-go credit balance |
| Rate Limit Reset Credits | Number of available rate-limit reset credits |
| Spend Limit Reached | Whether the spend limit has been reached (binary sensor) |
| `<model>` Usage / Reset Time | Usage and reset time of each additional per-model limit (e.g. GPT-5.3-Codex-Spark) |

Windows are identified by the `limit_window_seconds` duration in the
response (at least 1 day counts as the weekly window) instead of guessing
from the primary/secondary position; when a window is absent (for example
while OpenAI temporarily lifts the 5-hour limit), its sensors show as
unavailable. Additional per-model limits are discovered on first load;
limits the API adds later appear after reloading the integration or
restarting.

## Data quality

The usage endpoint occasionally returns transient outliers. The
integration first validates that percentages are finite numbers within
`0..100`; when a reading drops by at least 5 percentage points from the
last trusted value, or suddenly rises by at least 50, it waits 5 seconds
and takes one extra sample. A value is only published when the old and new
samples agree on a consistent trend; otherwise the usage shows as
`unknown` for that cycle, keeping outliers out of long-term statistics.

This check does not depend on the reset time, so an early reset can still
be confirmed by consecutive low samples. Normal polling adds no extra
requests, and when both windows look suspicious at once they share a
single confirmation request.

## Installation

Copy `custom_components/codex_usage` into Home Assistant's
`config/custom_components/` directory, then restart Home Assistant.
(You can also add this repository as a custom repository in HACS.)

## Configuration

1. Run `codex login` on the machine where you use the Codex CLI (if you
   are not logged in yet).
2. Open `~/.codex/auth.json` (`%USERPROFILE%\.codex\auth.json` on
   Windows) and copy its full contents.
3. In Home Assistant, go to **Settings → Devices & Services → Add
   Integration → Codex Usage** and paste that JSON.
4. If Home Assistant cannot reach OpenAI directly, set an outbound proxy
   in the setup form or the options (format `http://host:port`;
   `https://` proxies are not supported).

Home Assistant keeps its own copy of the tokens and renews them
automatically through the Codex CLI's public OAuth client; you will not
need to paste the JSON again unless the refresh token is revoked (the
integration will then prompt for re-authentication).

## Notes

- The usage endpoint is not a documented public API; OpenAI may change it
  at any time.
- A token refresh may cause the server to rotate the refresh token. If the
  local Codex CLI login stops working after setup, just run `codex login`
  again — the two sets of credentials are independent from then on.
