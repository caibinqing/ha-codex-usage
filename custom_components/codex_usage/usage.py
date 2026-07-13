"""Parse and validate Codex rate-limit usage samples."""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Mapping

USAGE_RESET_KEYS = {
    "session_usage_percent": "session_reset_time",
    "week_usage_percent": "week_reset_time",
}
USAGE_PERCENT_KEYS = tuple(USAGE_RESET_KEYS)

# Windows at least this long are the weekly limit; shorter ones are the
# 5-hour session limit. Position alone is unreliable: when OpenAI dropped the
# 5-hour window in July 2026, the weekly window moved into the primary slot.
WEEK_WINDOW_MIN_SECONDS = 24 * 3600

type UsageChangeKind = Literal["decrease", "increase", "invalid"]


def _window(limits: dict[str, Any], *keys: str) -> dict[str, Any] | None:
    """Return the first present rate-limit window under any given key."""
    for key in keys:
        window = limits.get(key)
        if isinstance(window, dict):
            return window
    return None


def _window_seconds(window: dict[str, Any]) -> float | None:
    """Extract a rate-limit window's duration in seconds, if advertised."""
    for key, scale in (("limit_window_seconds", 1), ("window_minutes", 60)):
        value = window.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
            return float(value) * scale
    return None


def _reset_time_iso(window: dict[str, Any], now: datetime | None = None) -> str | None:
    """Extract a rate-limit window reset time as an ISO timestamp."""
    for key in ("resets_at", "reset_at"):
        value = window.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            try:
                return datetime.fromtimestamp(value, UTC).isoformat()
            except (OSError, OverflowError, ValueError):
                return None
        if isinstance(value, str) and value:
            return value

    current_time = now or datetime.now(UTC)
    for key in ("resets_in_seconds", "reset_after_seconds"):
        value = window.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            try:
                seconds = float(value)
                if not math.isfinite(seconds):
                    return None
                return (current_time + timedelta(seconds=seconds)).isoformat()
            except (OverflowError, ValueError):
                return None
    return None


def _decimal_number(value: Any) -> float | None:
    """Parse a finite number that the API may send as a decimal string."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def normalize_usage_percent(value: Any) -> float | None:
    """Return a finite percentage in the inclusive 0..100 range."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        normalized = float(value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(normalized) or not 0 <= normalized <= 100:
        return None
    return normalized


def parse_usage(raw: Mapping[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Parse an API response into a flat, validated sensor data dictionary."""
    data: dict[str, Any] = {}

    limits = raw.get("rate_limits")
    if not isinstance(limits, dict) or not limits:
        legacy_limits = raw.get("rate_limit")
        if isinstance(legacy_limits, dict):
            limits = legacy_limits
    if not isinstance(limits, dict):
        limits = {}

    for position_keys, positional_bucket in (
        (("primary", "primary_window"), "session"),
        (("secondary", "secondary_window"), "week"),
    ):
        window = _window(limits, *position_keys)
        if window is None:
            continue
        seconds = _window_seconds(window)
        if seconds is None:
            bucket = positional_bucket
        else:
            bucket = "week" if seconds >= WEEK_WINDOW_MIN_SECONDS else "session"
        percent_key = f"{bucket}_usage_percent"
        if percent_key in data:
            continue
        data[percent_key] = normalize_usage_percent(window.get("used_percent"))
        data[USAGE_RESET_KEYS[percent_key]] = _reset_time_iso(window, now)

    plan = raw.get("plan_type")
    if plan:
        data["plan_type"] = str(plan)

    credits = raw.get("credits")
    if isinstance(credits, dict):
        data["credits_balance"] = _decimal_number(credits.get("balance"))

    reset_credits = raw.get("rate_limit_reset_credits")
    if isinstance(reset_credits, dict):
        count = reset_credits.get("available_count")
        data["reset_credits_available"] = (
            count
            if isinstance(count, int) and not isinstance(count, bool) and count >= 0
            else None
        )

    spend_control = raw.get("spend_control")
    if isinstance(spend_control, dict):
        reached = spend_control.get("reached")
        data["spend_limit_reached"] = reached if isinstance(reached, bool) else None

    additional = _parse_additional_limits(raw.get("additional_rate_limits"), now)
    if additional:
        data["additional_limits"] = additional

    return data


def _parse_additional_limits(
    entries: Any, now: datetime | None = None
) -> dict[str, dict[str, Any]]:
    """Parse per-model rate limits keyed by their stable feature slug."""
    limits: dict[str, dict[str, Any]] = {}
    if not isinstance(entries, list):
        return limits
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        feature = entry.get("metered_feature")
        if not isinstance(feature, str) or not feature or feature in limits:
            continue
        rate_limit = entry.get("rate_limit")
        if not isinstance(rate_limit, dict):
            continue
        window = _window(rate_limit, "primary", "primary_window")
        if window is None:
            continue
        name = entry.get("limit_name")
        limits[feature] = {
            "name": name if isinstance(name, str) and name else feature,
            "usage_percent": normalize_usage_percent(window.get("used_percent")),
            "reset_time": _reset_time_iso(window, now),
        }
    return limits


def _stable_reset_value(new_value: Any, old_value: Any, jitter_seconds: float) -> Any:
    """Return the old timestamp when the new one drifted less than the jitter."""
    if not new_value or not old_value:
        return new_value
    try:
        drift = (
            datetime.fromisoformat(new_value) - datetime.fromisoformat(old_value)
        ).total_seconds()
    except (ValueError, TypeError):
        return new_value
    return old_value if abs(drift) < jitter_seconds else new_value


def stabilize_reset_times(
    new: dict[str, Any],
    old: Mapping[str, Any] | None,
    jitter_seconds: float,
) -> None:
    """Suppress sub-threshold jitter in reset timestamps, in place."""
    if not old:
        return
    for key, new_value in new.items():
        if key.endswith("_reset_time"):
            new[key] = _stable_reset_value(new_value, old.get(key), jitter_seconds)

    new_limits = new.get("additional_limits")
    old_limits = old.get("additional_limits")
    if not isinstance(new_limits, dict) or not isinstance(old_limits, dict):
        return
    for feature, window in new_limits.items():
        old_window = old_limits.get(feature)
        if isinstance(window, dict) and isinstance(old_window, dict):
            window["reset_time"] = _stable_reset_value(
                window.get("reset_time"), old_window.get("reset_time"), jitter_seconds
            )


def usage_change_kind(
    old_value: Any,
    new_value: Any,
    *,
    drop_threshold: float,
    rise_threshold: float,
) -> UsageChangeKind | None:
    """Classify a change that needs a confirming API sample."""
    old_percent = normalize_usage_percent(old_value)
    if old_percent is None:
        return None

    new_percent = normalize_usage_percent(new_value)
    if new_percent is None:
        return "invalid"
    if old_percent - new_percent >= drop_threshold:
        return "decrease"
    if new_percent - old_percent >= rise_threshold:
        return "increase"
    return None


def suspicious_usage_keys(
    candidate: Mapping[str, Any],
    last_trusted: Mapping[str, float],
    *,
    drop_threshold: float,
    rise_threshold: float,
) -> set[str]:
    """Return usage keys whose candidate value needs a second sample."""
    return {
        key
        for key in USAGE_PERCENT_KEYS
        if key in last_trusted
        and usage_change_kind(
            last_trusted[key],
            candidate.get(key),
            drop_threshold=drop_threshold,
            rise_threshold=rise_threshold,
        )
        is not None
    }


def resolve_confirmed_usage(
    candidate: Mapping[str, Any],
    confirmation: Mapping[str, Any],
    last_trusted: Mapping[str, float],
    suspicious_keys: set[str],
    *,
    consensus_tolerance: float,
) -> dict[str, Any]:
    """Resolve suspicious fields using agreement between two of three samples.

    The previous trusted value, first candidate, and confirmation form the
    three samples.  The confirmation is accepted only when it is close to the
    candidate (a confirmed change) or to the previous value (a recovered
    outlier).  Otherwise that one field becomes unknown.
    """
    resolved = dict(candidate)
    for key in suspicious_keys:
        old_percent = normalize_usage_percent(last_trusted.get(key))
        candidate_percent = normalize_usage_percent(candidate.get(key))
        confirmed_percent = normalize_usage_percent(confirmation.get(key))

        has_consensus = confirmed_percent is not None and (
            (
                candidate_percent is not None
                and abs(confirmed_percent - candidate_percent) <= consensus_tolerance
            )
            or (
                old_percent is not None
                and abs(confirmed_percent - old_percent) <= consensus_tolerance
            )
        )
        resolved[key] = confirmed_percent if has_consensus else None

        reset_key = USAGE_RESET_KEYS[key]
        if has_consensus:
            resolved[reset_key] = confirmation.get(reset_key)
        else:
            resolved[reset_key] = None

    return resolved


async def async_resolve_usage_confirmation(
    candidate: Mapping[str, Any],
    confirmation_fetch: Callable[[], Awaitable[Mapping[str, Any]]],
    sleep: Callable[[float], Awaitable[None]],
    last_trusted: Mapping[str, float],
    suspicious_keys: set[str],
    *,
    delay_seconds: float,
    consensus_tolerance: float,
) -> tuple[dict[str, Any], Mapping[str, Any] | None]:
    """Fetch one shared confirmation and resolve each suspicious field."""
    if not suspicious_keys:
        return dict(candidate), None

    await sleep(delay_seconds)
    confirmation = await confirmation_fetch()
    return (
        resolve_confirmed_usage(
            candidate,
            confirmation,
            last_trusted,
            suspicious_keys,
            consensus_tolerance=consensus_tolerance,
        ),
        confirmation,
    )


def mark_usage_unknown(
    candidate: Mapping[str, Any], suspicious_keys: set[str]
) -> dict[str, Any]:
    """Return a copy with only suspicious usage values marked unknown."""
    result = dict(candidate)
    for key in suspicious_keys:
        result[key] = None
        result[USAGE_RESET_KEYS[key]] = None
    return result


def update_trusted_usage(trusted: dict[str, float], sample: Mapping[str, Any]) -> None:
    """Update the trusted baseline with valid values from a resolved sample."""
    for key in USAGE_PERCENT_KEYS:
        value = normalize_usage_percent(sample.get(key))
        if value is not None:
            trusted[key] = value
