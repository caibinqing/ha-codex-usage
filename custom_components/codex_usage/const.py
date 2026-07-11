"""Constants for Codex Usage integration."""

DOMAIN = "codex_usage"

# The public OAuth client id used by the Codex CLI; the refresh token from
# ~/.codex/auth.json can only be redeemed against this client.
OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"

# Same endpoint the Codex client polls for rate-limit windows. Not a public
# documented API, so the response is parsed defensively.
USAGE_API_URL = "https://chatgpt.com/backend-api/wham/usage"

DEFAULT_UPDATE_INTERVAL = 300  # seconds

# The rolling windows' reset timestamps drift by a few seconds between polls,
# which flips timestamp sensors across minute boundaries and floods the
# recorder. Keep the previous value unless the new one moved by more than this.
RESET_TIME_JITTER_SECONDS = 300

# A single unexpected usage sample is re-fetched before it reaches Home
# Assistant's recorder.  The thresholds deliberately target large, visible
# spikes while allowing small upstream corrections through unchanged.
USAGE_CONFIRM_DELAY_SECONDS = 5
USAGE_DROP_CONFIRM_THRESHOLD = 5.0
USAGE_RISE_CONFIRM_THRESHOLD = 50.0
USAGE_CONFIRM_CONSENSUS_TOLERANCE = 5.0

# Config keys
CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_ACCOUNT_ID = "account_id"
CONF_EXPIRES_AT = "expires_at"
CONF_UPDATE_INTERVAL = "update_interval"
# Optional outbound proxy (http://host:port) for every OpenAI request, for
# networks where Home Assistant has no direct egress. Stored in the entry
# options; also collected on the setup form so the first validation works.
CONF_PROXY_URL = "proxy_url"

# Sensor definitions: (key, name, unit, icon, device_class)
SENSOR_DEFINITIONS = [
    ("session_usage_percent", "5-Hour Usage", "%", "mdi:timer-sand", None),
    ("session_reset_time", "5-Hour Reset Time", None, "mdi:timer-refresh", "timestamp"),
    ("week_usage_percent", "Weekly Usage", "%", "mdi:calendar-week", None),
    ("week_reset_time", "Weekly Reset Time", None, "mdi:calendar-clock", "timestamp"),
    ("plan_type", "Plan Type", None, "mdi:card-account-details", None),
]
