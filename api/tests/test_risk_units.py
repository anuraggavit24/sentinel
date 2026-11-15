import ipaddress

import pytest

from app.net import client_ip, parse_networks
from app.risk.normalize import PayeeError, normalise
from app.risk.rules import ALLOW, SANDBOX, STEP_UP, Signals, score

KEY = "consortium-demo-key-v1"


# ---------------------------------------------------------------- normalisation

def test_account_normalisation_matches_scenario4_hashing():
    p = normalise("ACCOUNT", {"ifsc": " hdfc0000123 ", "account": "5010 0012 3456"})
    assert p.normalised == "HDFC0000123|501000123456"
    # scenario4.sh: printf 'HDFC0000123|501000123456' | openssl dgst -sha256 -hmac consortium-demo-key-v1
    import hashlib, hmac
    assert p.id_hash(KEY) == hmac.new(KEY.encode(), b"HDFC0000123|501000123456", hashlib.sha256).hexdigest()
    assert "3456" in p.display and "5010" not in p.display


def test_same_digits_at_different_banks_hash_differently():
    a = normalise("ACCOUNT", {"ifsc": "HDFC0000123", "account": "123456789"})
    b = normalise("ACCOUNT", {"ifsc": "SBIN0000456", "account": "123456789"})
    assert a.id_hash(KEY) != b.id_hash(KEY)


@pytest.mark.parametrize("raw", ["+91 98765 43210", "919876543210", "09876543210", "98765-43210", "0091 9876543210"])
def test_phone_formats_normalise_to_e164(raw):
    assert normalise("PHONE", {"phone": raw}).normalised == "+919876543210"


@pytest.mark.parametrize(
    "ptype,fields",
    [
        ("ACCOUNT", {"ifsc": "HDFC123", "account": "123456789"}),
        ("ACCOUNT", {"ifsc": "HDFC0000123", "account": "12ab"}),
        ("PHONE", {"phone": "5876543210"}),
        ("PHONE", {"phone": "98765"}),
        ("UPI", {"upi": "no-at-sign"}),
        ("CARD", {}),
    ],
)
def test_invalid_payees_are_rejected(ptype, fields):
    with pytest.raises(PayeeError):
        normalise(ptype, fields)


def test_upi_is_lowercased_and_masked():
    p = normalise("UPI", {"upi": " Anurag.G@OKHDFC "})
    assert p.normalised == "anurag.g@okhdfc" and p.display.endswith("@okhdfc") and "anurag" not in p.display


# ---------------------------------------------------------------- rules

def base(**kw) -> Signals:
    d = dict(amount_paise=500_00, hour_ist=14, payee_type="UPI", payee_seen_before=True)
    d.update(kw)
    return Signals(**d)


def test_ordinary_payment_to_known_payee_is_allowed_with_no_reasons():
    d = score(base())
    assert d.score == 0 and d.band == ALLOW and d.reasons == []


def test_new_payee_alone_is_allowed_but_explained():
    d = score(base(payee_seen_before=False))
    assert d.band == ALLOW and [r.code for r in d.reasons] == ["NEW_PAYEE"]


def test_amount_relative_to_history():
    history = [500_00] * 6
    assert "AMOUNT_ABOVE_USUAL" in [r.code for r in score(base(amount_paise=2_000_00, history_paise=history)).reasons]
    assert "AMOUNT_FAR_ABOVE_USUAL" in [r.code for r in score(base(amount_paise=6_000_00, history_paise=history)).reasons]
    # with too little history, only large absolute amounts count
    assert score(base(amount_paise=6_000_00, history_paise=[500_00])).reasons == []


def test_fri_high_risk_payee_number():
    d = score(base(payee_type="PHONE", fri_category="VERY_HIGH"))
    assert d.reasons[0].code == "FRI_VERY_HIGH" and d.score == 45 and d.band == STEP_UP


def test_account_takeover_goes_to_sandbox():
    d = score(base(payee_seen_before=False, amount_paise=40_000_00, sim_swap_hours_ago=3, new_device=True))
    assert d.band == SANDBOX and d.takeover_signals and d.score >= 70


def test_high_score_without_takeover_signals_is_never_sandbox():
    # Coached customer: lots of risk, but it is the real customer on their own device.
    d = score(base(payee_seen_before=False, amount_paise=90_000_00, active_call=True, screen_sharing=True))
    assert d.score >= 70 and d.coaching_signals and not d.takeover_signals
    assert d.band == STEP_UP
    assert "CALL_NEW_PAYEE_LARGE" in [r.code for r in d.reasons]


def test_sim_swap_without_new_device_is_step_up_not_sandbox():
    d = score(base(payee_seen_before=False, amount_paise=90_000_00, sim_swap_hours_ago=2, integrity="FAIL"))
    assert d.score >= 70 and d.band == STEP_UP


def test_score_is_capped_and_reasons_sorted():
    d = score(base(payee_seen_before=False, amount_paise=3_00_000_00, hour_ist=2, payments_last_10_min=5,
                   fri_category="VERY_HIGH", sim_swap_hours_ago=1, new_device=True, integrity="FAIL",
                   active_call=True, screen_sharing=True, accessibility_services=True, synced_passkey=True))
    assert d.score == 100
    pts = [r.points for r in d.reasons]
    assert pts == sorted(pts, reverse=True)


def test_old_sim_swap_does_not_count():
    assert score(base(sim_swap_hours_ago=200)).reasons == []


# ---------------------------------------------------------------- client IP

TRUSTED = parse_networks("127.0.0.1/32,172.16.0.0/12")


def test_untrusted_peer_cannot_spoof_its_ip():
    assert client_ip("203.0.113.9", "1.2.3.4", TRUSTED) == "203.0.113.9"


def test_trusted_proxy_forwards_the_real_client():
    assert client_ip("172.20.0.1", "49.36.10.20", TRUSTED) == "49.36.10.20"


def test_spoofed_header_behind_proxy_takes_rightmost_untrusted_hop():
    # attacker sends X-Forwarded-For: 1.2.3.4 ; the proxy appends the real address
    assert client_ip("172.20.0.1", "1.2.3.4, 49.36.10.20", TRUSTED) == "49.36.10.20"


def test_local_demo_all_hops_trusted_and_ipv4_mapped_addresses():
    assert client_ip("172.20.0.1", "::ffff:127.0.0.1", TRUSTED) == "127.0.0.1"
    assert client_ip("::ffff:203.0.113.9", None, TRUSTED) == "203.0.113.9"
