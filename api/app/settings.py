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
