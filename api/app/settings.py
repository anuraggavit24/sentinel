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
