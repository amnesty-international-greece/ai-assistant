"""Constants for the Discord integration — every magic number lives here."""
from __future__ import annotations

# ── Discord platform limits ──────────────────────────────────────────────────
DISCORD_MESSAGE_MAX_CHARS = 2000
DISCORD_EMBED_DESCRIPTION_MAX = 4096
DISCORD_THREAD_NAME_MAX = 100

# Safe send limit used by split_message — leaves 100-char headroom for suffixes
# ("..." continuation marker) appended by the message splitter.
DISCORD_MESSAGE_SAFE_CHARS = 1900

# ── Email gateway ────────────────────────────────────────────────────────────
EMAIL_POLL_INTERVAL_SECONDS = 60
EMAIL_TEST_POLL_INTERVAL_SECONDS = 30      # 30 s avoids Gmail IMAP throttling; still faster than production 60 s
EMAIL_ATTACHMENT_MAX_BYTES = 5 * 1024 * 1024   # 5 MB
EMAIL_SUBJECT_PREVIEW_MAX_CHARS = 100

# IMAP folder to poll and UNSEEN search criterion
EMAIL_IMAP_FOLDER = "INBOX"
EMAIL_IMAP_SEARCH_CRITERION = "UNSEEN"

# Display name used in the From header of outbound emails
EMAIL_SENDER_DISPLAY_NAME = "Forum Assistant"

# Encoding fallback order when decoding email text parts
EMAIL_DECODE_CHARSETS = ("utf-8", "latin-1")

# Classifier sends only the first N chars of the body to Gemini to save tokens.
EMAIL_BODY_CLASSIFY_PREVIEW_CHARS = 500

# Domain used in synthetic Message-IDs for outbound emails.
# Format: <{discord_thread_id}@EMAIL_MESSAGE_ID_DOMAIN>
EMAIL_MESSAGE_ID_DOMAIN = "forum.amnesty-international-greece"

# ── Classifier ───────────────────────────────────────────────────────────────
CLASSIFIER_CONFIDENCE_THRESHOLD = 0.70    # Below this → UNCERTAIN
CLASSIFIER_TEMPERATURE = 0.1
CLASSIFIER_MAX_OUTPUT_TOKENS = 50         # Gemini response cap for tag name
CLASSIFIER_MODEL = "gemini-2.0-flash"
CLASSIFIER_UNCERTAIN_LABEL = "UNCERTAIN"

# ── Stats / scheduling ───────────────────────────────────────────────────────
WEEKLY_DIGEST_DAY = 6     # Sunday (0=Monday … 6=Sunday, matches Python weekday())
WEEKLY_DIGEST_HOUR = 0    # midnight Europe/Athens
# Guard: don't re-send digest if already sent within the last N seconds.
WEEKLY_DIGEST_MIN_INTERVAL_SECONDS = 3600

# ── Download retry ───────────────────────────────────────────────────────────
ATTACHMENT_DOWNLOAD_MAX_RETRIES = 3
# Exponential back-off base (seconds): attempt 1→2s, 2→4s, 3→8s
ATTACHMENT_DOWNLOAD_BACKOFF_BASE = 2

# ── Bot restart / watchdog ───────────────────────────────────────────────────
RESTART_INITIAL_DELAY_SECONDS = 60
RESTART_MAX_DELAY_SECONDS = 1800          # Cap back-off at 30 minutes

# ── Config-watcher polling (legacy self-setup flow) ──────────────────────────
CONFIG_WATCH_CHECK_INTERVAL_SECONDS = 2   # How often to re-read config.env while waiting

# ── Pending-email admin thread ───────────────────────────────────────────────
# How many messages to scan in admin channel history when cleaning up a pending thread.
ADMIN_HISTORY_SCAN_LIMIT = 50

# ── State keys (primary keys in discord_bot_state table) ────────────────────
STATE_BOT_ACTIVE = "bot_active"
STATE_WEBHOOK_ACTIVE = "webhook_active"
STATE_AUTO_CLASSIFY = "auto_classify"
STATE_TEST_MODE_ACTIVE = "test_mode_active"
STATE_TEST_EMAIL = "test_email"

# ── Webhook name expected on Discord forum channels ──────────────────────────
WEBHOOK_NAME = "Event_Info"

# ── Audit / log workflow tag ─────────────────────────────────────────────────
WORKFLOW_NAME = "discord"
