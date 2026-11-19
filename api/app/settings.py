import os

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://sentinel:sentinel@db:5432/sentinel")
RP_ID = os.environ.get("RP_ID", "localhost")
RP_NAME = os.environ.get("RP_NAME", "Sentinel Bank")
# Origins the browser may use. Vite dev server runs on 5173; it proxies /api to this service.
RP_ORIGINS = [o.strip() for o in os.environ.get("RP_ORIGINS", "http://localhost:5173").split(",") if o.strip()]
CHALLENGE_TTL_SECONDS = int(os.environ.get("CHALLENGE_TTL_SECONDS", "300"))
SESSION_TTL_HOURS = int(os.environ.get("SESSION_TTL_HOURS", "12"))
COOKIE_NAME = "sentinel_session"
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"  # set to 1 behind HTTPS
AUDIT_URL = os.environ.get("AUDIT_URL", "")  # e.g. http://audit-api:8800 ; empty disables audit events
AUDIT_STREAM = os.environ.get("AUDIT_STREAM", "BankAMSP-auth")
# Shared secret the audit-log service requires on writes. Must match the audit service's AUDIT_TOKEN.
AUDIT_TOKEN = os.environ.get("AUDIT_TOKEN", "")
AUDIT_DRAIN_INTERVAL_SECONDS = float(os.environ.get("AUDIT_DRAIN_INTERVAL_SECONDS", "1.0"))
# Proxies whose X-Forwarded-For header is believed: loopback and Docker's private ranges.
TRUSTED_PROXIES = os.environ.get("TRUSTED_PROXIES", "127.0.0.1/32,::1/128,172.16.0.0/12")
# Same normalisation + key as the consortium ledger, so payee hashes match other banks' reports.
# No default: payments are refused (503) until it is configured. A key in code is a key in Git.
PAYEE_HMAC_KEY = os.environ.get("PAYEE_HMAC_KEY", "")
# Enables the demo signal injector (SIM swap, new device, call, screen sharing...). Never on in production.
DEMO_SIGNALS = os.environ.get("DEMO_SIGNALS", "0") == "1"
# The injector is an operator tool: it needs this token, so a session can never edit its own signals.
DEMO_OPERATOR_TOKEN = os.environ.get("DEMO_OPERATOR_TOKEN", "")
# Adds the score, points and FRI category to payment responses. Demo/analyst only: for a customer this
# turns the payment form into a risk-rating lookup tool.
SHOW_ANALYST_VIEW = os.environ.get("SHOW_ANALYST_VIEW", "0") == "1"
# Volunteer typing study (slice 2b part 2). Off unless enabled. Not linked to bank accounts.
STUDY_ENABLED = os.environ.get("STUDY_ENABLED", "0") == "1"
# Minimum gap between a volunteer's study sessions, so genuine attempts include day-to-day drift.
STUDY_SESSION_GAP_HOURS = float(os.environ.get("STUDY_SESSION_GAP_HOURS", "4"))
# Consortium ledger bridge (slice 2c). Empty URL = not connected; payments then score without it.
LEDGER_URL = os.environ.get("LEDGER_URL", "").rstrip("/")
LEDGER_TOKEN = os.environ.get("LEDGER_TOKEN", "")
# A payment waits at most this long for the ledger, then scores without it (fail-open, recorded).
LEDGER_TIMEOUT_MS = int(os.environ.get("LEDGER_TIMEOUT_MS", "800"))
# During a consortium key rotation, payees are also looked up under the previous key's hash.
PAYEE_HMAC_KEY_PREVIOUS = os.environ.get("PAYEE_HMAC_KEY_PREVIOUS", "")
# Module 3a: ledger writes (reportMule / confirmReport). Empty = analysts can decide, but nothing is filed.
LEDGER_WRITE_TOKEN = os.environ.get("LEDGER_WRITE_TOKEN", "")
LEDGER_MSP = os.environ.get("LEDGER_MSP", "BankAMSP")  # this bank, as the ledger knows it
LEDGER_DRAIN_INTERVAL_SECONDS = float(os.environ.get("LEDGER_DRAIN_INTERVAL_SECONDS", "3"))
OPS_ELEVATION_MINUTES = int(os.environ.get("OPS_ELEVATION_MINUTES", "10"))
# Module 4: analyst copilot. Without a key it answers in extractive mode (quoted passages, no model).
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
COPILOT_MODEL = os.environ.get("COPILOT_MODEL", "claude-sonnet-5-5")
COPILOT_MAX_TOKENS = int(os.environ.get("COPILOT_MAX_TOKENS", "700"))
COPILOT_LLM = os.environ.get("COPILOT_LLM", "auto")  # auto | off
# Demo bank network (#/demo): three banks, one-click logins WITHOUT passkeys. Never on in production.
DEMO_LOGIN = os.environ.get("DEMO_LOGIN", "0") == "1"
