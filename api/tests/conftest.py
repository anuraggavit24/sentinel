"""Test configuration. Set before the app is imported: the app refuses to run payments without a key."""
import os

os.environ.setdefault("PAYEE_HMAC_KEY", "consortium-demo-key-v1")
os.environ.setdefault("DEMO_OPERATOR_TOKEN", "test-operator-token")
