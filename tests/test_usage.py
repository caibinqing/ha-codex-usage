"""Tests for Codex usage parsing and sample confirmation."""

from __future__ import annotations

import importlib.util
import math
from datetime import UTC, datetime
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock

MODULE_PATH = (
    Path(__file__).parents[1] / "custom_components" / "codex_usage" / "usage.py"
)
SPEC = importlib.util.spec_from_file_location("codex_usage_usage", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
usage = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = usage
SPEC.loader.exec_module(usage)

DROP_THRESHOLD = 5.0
RISE_THRESHOLD = 50.0
CONSENSUS_TOLERANCE = 5.0


class NormalizeUsagePercentTests(unittest.TestCase):
    """Test percentage validation."""

    def test_accepts_range_boundaries(self) -> None:
        self.assertEqual(usage.normalize_usage_percent(0), 0.0)
        self.assertEqual(usage.normalize_usage_percent(100), 100.0)
        self.assertEqual(usage.normalize_usage_percent(12.5), 12.5)

    def test_rejects_invalid_values(self) -> None:
        invalid_values = (
            None,
            True,
            "20",
            -1,
            101,
            math.nan,
            math.inf,
            -math.inf,
            10**1000,
        )
        for value in invalid_values:
            with self.subTest(value=value):
                self.assertIsNone(usage.normalize_usage_percent(value))


class ParseUsageTests(unittest.TestCase):
    """Test defensive parsing of the undocumented API response."""

    def test_parses_both_windows_and_relative_reset(self) -> None:
        now = datetime(2026, 7, 11, 8, 0, tzinfo=UTC)
        result = usage.parse_usage(
            {
                "rate_limits": {
                    "primary": {
                        "used_percent": 12,
                        "resets_in_seconds": 60,
                    },
                    "secondary_window": {
                        "used_percent": 34.5,
                        "reset_at": "2026-07-18T08:00:00+00:00",
                    },
                },
                "plan_type": "plus",
            },
            now=now,
        )

        self.assertEqual(result["session_usage_percent"], 12.0)
        self.assertEqual(result["session_reset_time"], "2026-07-11T08:01:00+00:00")
        self.assertEqual(result["week_usage_percent"], 34.5)
        self.assertEqual(result["week_reset_time"], "2026-07-18T08:00:00+00:00")
        self.assertEqual(result["plan_type"], "plus")

    def test_invalid_percent_becomes_unknown_not_zero(self) -> None:
        result = usage.parse_usage(
            {"rate_limit": {"primary_window": {"used_percent": "0"}}}
        )

        self.assertIn("session_usage_percent", result)
        self.assertIsNone(result["session_usage_percent"])

    def test_ignores_malformed_limits(self) -> None:
        self.assertEqual(usage.parse_usage({"rate_limits": "broken"}), {})

    def test_falls_back_from_malformed_rate_limits(self) -> None:
        result = usage.parse_usage(
            {
                "rate_limits": "broken",
                "rate_limit": {"primary": {"used_percent": 12}},
            }
        )

        self.assertEqual(result["session_usage_percent"], 12.0)

    def test_falls_back_from_empty_rate_limits(self) -> None:
        result = usage.parse_usage(
            {
                "rate_limits": {},
                "rate_limit": {"primary": {"used_percent": 12}},
            }
        )

        self.assertEqual(result["session_usage_percent"], 12.0)

    def test_weekly_primary_window_maps_to_week_sensors(self) -> None:
        # Live shape from 2026-07: the 5-hour window was removed and the
        # weekly window moved into the primary slot.
        result = usage.parse_usage(
            {
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 11,
                        "limit_window_seconds": 604800,
                        "reset_after_seconds": 596056,
                        "reset_at": 1784511343,
                    },
                    "secondary_window": None,
                },
                "plan_type": "pro",
            }
        )

        self.assertEqual(result["week_usage_percent"], 11.0)
        self.assertEqual(result["week_reset_time"], "2026-07-20T01:35:43+00:00")
        self.assertNotIn("session_usage_percent", result)
        self.assertNotIn("session_reset_time", result)

    def test_window_duration_overrides_position(self) -> None:
        result = usage.parse_usage(
            {
                "rate_limits": {
                    "primary": {
                        "used_percent": 11,
                        "limit_window_seconds": 604800,
                    },
                    "secondary": {
                        "used_percent": 42,
                        "limit_window_seconds": 18000,
                    },
                }
            }
        )

        self.assertEqual(result["week_usage_percent"], 11.0)
        self.assertEqual(result["session_usage_percent"], 42.0)

    def test_window_minutes_duration_is_recognized(self) -> None:
        result = usage.parse_usage(
            {
                "rate_limits": {
                    "primary": {"used_percent": 5, "window_minutes": 300},
                    "secondary": {"used_percent": 30, "window_minutes": 10080},
                }
            }
        )

        self.assertEqual(result["session_usage_percent"], 5.0)
        self.assertEqual(result["week_usage_percent"], 30.0)

    def test_duplicate_bucket_keeps_first_window(self) -> None:
        result = usage.parse_usage(
            {
                "rate_limits": {
                    "primary": {
                        "used_percent": 11,
                        "limit_window_seconds": 604800,
                    },
                    "secondary": {
                        "used_percent": 99,
                        "limit_window_seconds": 604800,
                    },
                }
            }
        )

        self.assertEqual(result["week_usage_percent"], 11.0)
        self.assertNotIn("session_usage_percent", result)

    def test_invalid_duration_falls_back_to_position(self) -> None:
        result = usage.parse_usage(
            {
                "rate_limits": {
                    "primary": {"used_percent": 5, "limit_window_seconds": True},
                    "secondary": {"used_percent": 30, "limit_window_seconds": "week"},
                }
            }
        )

        self.assertEqual(result["session_usage_percent"], 5.0)
        self.assertEqual(result["week_usage_percent"], 30.0)

    def test_rejects_overflowing_relative_reset(self) -> None:
        result = usage.parse_usage(
            {
                "rate_limits": {
                    "primary": {
                        "used_percent": 12,
                        "resets_in_seconds": 10**1000,
                    }
                }
            }
        )

        self.assertIsNone(result["session_reset_time"])


class ParseExtraFieldsTests(unittest.TestCase):
    """Test parsing of credits, reset credits, spend control, extra limits."""

    def test_parses_live_2026_07_payload(self) -> None:
        result = usage.parse_usage(
            {
                "plan_type": "pro",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 11,
                        "limit_window_seconds": 604800,
                        "reset_at": 1784511343,
                    },
                    "secondary_window": None,
                },
                "additional_rate_limits": [
                    {
                        "limit_name": "GPT-5.3-Codex-Spark",
                        "metered_feature": "codex_bengalfox",
                        "rate_limit": {
                            "primary_window": {
                                "used_percent": 0,
                                "limit_window_seconds": 604800,
                                "reset_at": 1784520087,
                            },
                            "secondary_window": None,
                        },
                    }
                ],
                "credits": {"has_credits": False, "balance": "0"},
                "spend_control": {"reached": False, "individual_limit": None},
                "rate_limit_reset_credits": {"available_count": 4},
            }
        )

        self.assertEqual(result["credits_balance"], 0.0)
        self.assertEqual(result["reset_credits_available"], 4)
        self.assertIs(result["spend_limit_reached"], False)
        self.assertEqual(
            result["additional_limits"],
            {
                "codex_bengalfox": {
                    "name": "GPT-5.3-Codex-Spark",
                    "usage_percent": 0.0,
                    "reset_time": "2026-07-20T04:01:27+00:00",
                }
            },
        )

    def test_absent_sections_produce_no_keys(self) -> None:
        result = usage.parse_usage({"rate_limit": {}})

        for key in (
            "credits_balance",
            "reset_credits_available",
            "spend_limit_reached",
            "additional_limits",
        ):
            self.assertNotIn(key, result)

    def test_invalid_extra_values_become_unknown(self) -> None:
        result = usage.parse_usage(
            {
                "credits": {"balance": "not-a-number"},
                "rate_limit_reset_credits": {"available_count": True},
                "spend_control": {"reached": "yes"},
            }
        )

        self.assertIsNone(result["credits_balance"])
        self.assertIsNone(result["reset_credits_available"])
        self.assertIsNone(result["spend_limit_reached"])

    def test_negative_reset_credit_count_is_rejected(self) -> None:
        result = usage.parse_usage(
            {"rate_limit_reset_credits": {"available_count": -1}}
        )

        self.assertIsNone(result["reset_credits_available"])

    def test_malformed_additional_limit_entries_are_skipped(self) -> None:
        result = usage.parse_usage(
            {
                "additional_rate_limits": [
                    "broken",
                    {"limit_name": "No Feature"},
                    {"metered_feature": "no_rate_limit"},
                    {"metered_feature": "no_window", "rate_limit": {}},
                    {
                        "metered_feature": "unnamed",
                        "rate_limit": {"primary_window": {"used_percent": 7}},
                    },
                    {
                        "metered_feature": "unnamed",
                        "rate_limit": {"primary_window": {"used_percent": 99}},
                    },
                ]
            }
        )

        self.assertEqual(
            result["additional_limits"],
            {
                "unnamed": {
                    "name": "unnamed",
                    "usage_percent": 7.0,
                    "reset_time": None,
                }
            },
        )


class ConfirmationTests(unittest.TestCase):
    """Test change detection and two-of-three sample agreement."""

    def _resolve(self, old: float, first: object, second: object) -> object:
        key = "session_usage_percent"
        candidate = {key: first, "session_reset_time": "first-reset"}
        confirmation = {key: second, "session_reset_time": "second-reset"}
        trusted = {key: old}
        suspicious = usage.suspicious_usage_keys(
            candidate,
            trusted,
            drop_threshold=DROP_THRESHOLD,
            rise_threshold=RISE_THRESHOLD,
        )
        self.assertEqual(suspicious, {key})
        result = usage.resolve_confirmed_usage(
            candidate,
            confirmation,
            trusted,
            suspicious,
            consensus_tolerance=CONSENSUS_TOLERANCE,
        )
        if result[key] is not None:
            self.assertEqual(result["session_reset_time"], "second-reset")
        return result[key]

    def test_confirms_early_reset(self) -> None:
        self.assertEqual(self._resolve(90, 5, 6), 6.0)

    def test_discards_one_off_low_value(self) -> None:
        self.assertEqual(self._resolve(90, 5, 92), 92.0)

    def test_ambiguous_low_values_become_unknown(self) -> None:
        key = "session_usage_percent"
        result = usage.resolve_confirmed_usage(
            {key: 5, "session_reset_time": "first-reset"},
            {key: 50, "session_reset_time": "second-reset"},
            {key: 90},
            {key},
            consensus_tolerance=CONSENSUS_TOLERANCE,
        )

        self.assertIsNone(result[key])
        self.assertIsNone(result["session_reset_time"])

    def test_discards_one_off_high_value(self) -> None:
        self.assertEqual(self._resolve(10, 95, 11), 11.0)

    def test_confirms_large_real_increase(self) -> None:
        self.assertEqual(self._resolve(10, 95, 96), 96.0)

    def test_invalid_first_value_needs_normal_confirmation(self) -> None:
        self.assertEqual(self._resolve(90, None, 91), 91.0)
        self.assertIsNone(self._resolve(90, None, 5))

    def test_small_change_does_not_trigger_confirmation(self) -> None:
        suspicious = usage.suspicious_usage_keys(
            {"session_usage_percent": 21},
            {"session_usage_percent": 20},
            drop_threshold=DROP_THRESHOLD,
            rise_threshold=RISE_THRESHOLD,
        )
        self.assertEqual(suspicious, set())

    def test_confirmation_threshold_boundaries(self) -> None:
        trusted = {"session_usage_percent": 50}

        at_drop_threshold = usage.suspicious_usage_keys(
            {"session_usage_percent": 45},
            trusted,
            drop_threshold=DROP_THRESHOLD,
            rise_threshold=RISE_THRESHOLD,
        )
        below_drop_threshold = usage.suspicious_usage_keys(
            {"session_usage_percent": 45.001},
            trusted,
            drop_threshold=DROP_THRESHOLD,
            rise_threshold=RISE_THRESHOLD,
        )
        at_rise_threshold = usage.suspicious_usage_keys(
            {"session_usage_percent": 100},
            trusted,
            drop_threshold=DROP_THRESHOLD,
            rise_threshold=RISE_THRESHOLD,
        )
        below_rise_threshold = usage.suspicious_usage_keys(
            {"session_usage_percent": 99.999},
            trusted,
            drop_threshold=DROP_THRESHOLD,
            rise_threshold=RISE_THRESHOLD,
        )

        self.assertEqual(at_drop_threshold, {"session_usage_percent"})
        self.assertEqual(below_drop_threshold, set())
        self.assertEqual(at_rise_threshold, {"session_usage_percent"})
        self.assertEqual(below_rise_threshold, set())

    def test_consensus_tolerance_boundary(self) -> None:
        key = "session_usage_percent"
        common = (
            {key: 5},
            {key: 90},
            {key},
        )
        at_tolerance = usage.resolve_confirmed_usage(
            common[0],
            {key: 10},
            common[1],
            common[2],
            consensus_tolerance=CONSENSUS_TOLERANCE,
        )
        outside_tolerance = usage.resolve_confirmed_usage(
            common[0],
            {key: 10.001},
            common[1],
            common[2],
            consensus_tolerance=CONSENSUS_TOLERANCE,
        )

        self.assertEqual(at_tolerance[key], 10.0)
        self.assertIsNone(outside_tolerance[key])

    def test_windows_are_resolved_independently(self) -> None:
        candidate = {
            "session_usage_percent": 5,
            "session_reset_time": "session-first",
            "week_usage_percent": 31,
            "week_reset_time": "week-first",
            "plan_type": "plus",
        }
        confirmation = {
            "session_usage_percent": 6,
            "session_reset_time": "session-second",
            "week_usage_percent": 99,
            "week_reset_time": "week-second",
            "plan_type": "plus",
        }
        trusted = {"session_usage_percent": 90, "week_usage_percent": 30}
        suspicious = usage.suspicious_usage_keys(
            candidate,
            trusted,
            drop_threshold=DROP_THRESHOLD,
            rise_threshold=RISE_THRESHOLD,
        )
        result = usage.resolve_confirmed_usage(
            candidate,
            confirmation,
            trusted,
            suspicious,
            consensus_tolerance=CONSENSUS_TOLERANCE,
        )

        self.assertEqual(suspicious, {"session_usage_percent"})
        self.assertEqual(result["session_usage_percent"], 6.0)
        self.assertEqual(result["session_reset_time"], "session-second")
        self.assertEqual(result["week_usage_percent"], 31)
        self.assertEqual(result["week_reset_time"], "week-first")
        self.assertEqual(result["plan_type"], "plus")

    def test_unknown_does_not_replace_trusted_baseline(self) -> None:
        trusted = {"session_usage_percent": 90.0}
        usage.update_trusted_usage(
            trusted,
            {"session_usage_percent": None, "week_usage_percent": 25},
        )

        self.assertEqual(trusted["session_usage_percent"], 90.0)
        self.assertEqual(trusted["week_usage_percent"], 25.0)

    def test_confirmation_failure_marks_only_suspicious_pair_unknown(self) -> None:
        result = usage.mark_usage_unknown(
            {
                "session_usage_percent": 5,
                "session_reset_time": "bad-reset",
                "week_usage_percent": 30,
                "week_reset_time": "good-reset",
            },
            {"session_usage_percent"},
        )

        self.assertIsNone(result["session_usage_percent"])
        self.assertIsNone(result["session_reset_time"])
        self.assertEqual(result["week_usage_percent"], 30)
        self.assertEqual(result["week_reset_time"], "good-reset")


class AsyncConfirmationTests(unittest.IsolatedAsyncioTestCase):
    """Test that suspicious fields share a single conditional re-fetch."""

    async def test_normal_sample_does_not_wait_or_fetch(self) -> None:
        fetch = AsyncMock()
        sleep = AsyncMock()
        candidate = {"session_usage_percent": 21}

        result, confirmation = await usage.async_resolve_usage_confirmation(
            candidate,
            fetch,
            sleep,
            {"session_usage_percent": 20},
            set(),
            delay_seconds=5,
            consensus_tolerance=CONSENSUS_TOLERANCE,
        )

        self.assertEqual(result, candidate)
        self.assertIsNone(confirmation)
        fetch.assert_not_awaited()
        sleep.assert_not_awaited()

    async def test_two_suspicious_windows_share_one_confirmation(self) -> None:
        candidate = {
            "session_usage_percent": 5,
            "session_reset_time": "first-session-reset",
            "week_usage_percent": 2,
            "week_reset_time": "first-week-reset",
        }
        confirmation_sample = {
            "session_usage_percent": 6,
            "session_reset_time": "second-session-reset",
            "week_usage_percent": 3,
            "week_reset_time": "second-week-reset",
        }
        fetch = AsyncMock(return_value=confirmation_sample)
        sleep = AsyncMock()

        result, confirmation = await usage.async_resolve_usage_confirmation(
            candidate,
            fetch,
            sleep,
            {"session_usage_percent": 90, "week_usage_percent": 80},
            {"session_usage_percent", "week_usage_percent"},
            delay_seconds=5,
            consensus_tolerance=CONSENSUS_TOLERANCE,
        )

        self.assertIs(confirmation, confirmation_sample)
        self.assertEqual(result["session_usage_percent"], 6.0)
        self.assertEqual(result["week_usage_percent"], 3.0)
        sleep.assert_awaited_once_with(5)
        fetch.assert_awaited_once_with()

    async def test_confirmation_error_propagates_to_caller(self) -> None:
        fetch = AsyncMock(side_effect=RuntimeError("failed"))
        sleep = AsyncMock()

        with self.assertRaisesRegex(RuntimeError, "failed"):
            await usage.async_resolve_usage_confirmation(
                {"session_usage_percent": 5},
                fetch,
                sleep,
                {"session_usage_percent": 90},
                {"session_usage_percent"},
                delay_seconds=5,
                consensus_tolerance=CONSENSUS_TOLERANCE,
            )

        sleep.assert_awaited_once_with(5)
        fetch.assert_awaited_once_with()


class ResetTimeStabilizationTests(unittest.TestCase):
    """Keep the pre-existing reset timestamp jitter behavior covered."""

    def test_small_reset_time_drift_keeps_previous_value(self) -> None:
        old = {"session_reset_time": "2026-07-11T08:00:00+00:00"}
        new = {"session_reset_time": "2026-07-11T08:04:59+00:00"}

        usage.stabilize_reset_times(new, old, jitter_seconds=300)

        self.assertEqual(new["session_reset_time"], old["session_reset_time"])

    def test_large_reset_time_change_is_preserved(self) -> None:
        old = {"session_reset_time": "2026-07-11T08:00:00+00:00"}
        new = {"session_reset_time": "2026-07-11T08:05:00+00:00"}

        usage.stabilize_reset_times(new, old, jitter_seconds=300)

        self.assertEqual(new["session_reset_time"], "2026-07-11T08:05:00+00:00")

    def test_additional_limit_reset_times_are_stabilized(self) -> None:
        old = {
            "additional_limits": {
                "codex_bengalfox": {"reset_time": "2026-07-20T04:00:00+00:00"},
            }
        }
        new = {
            "additional_limits": {
                "codex_bengalfox": {"reset_time": "2026-07-20T04:01:27+00:00"},
                "new_feature": {"reset_time": "2026-07-21T00:00:00+00:00"},
            }
        }

        usage.stabilize_reset_times(new, old, jitter_seconds=300)

        limits = new["additional_limits"]
        self.assertEqual(
            limits["codex_bengalfox"]["reset_time"], "2026-07-20T04:00:00+00:00"
        )
        self.assertEqual(
            limits["new_feature"]["reset_time"], "2026-07-21T00:00:00+00:00"
        )


if __name__ == "__main__":
    unittest.main()
