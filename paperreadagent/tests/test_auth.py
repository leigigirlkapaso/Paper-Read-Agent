def test_password_hash_and_verify():
    from paperreadagent.web.auth import hash_password, verify_password
    pw = "test-password-123"
    hashed = hash_password(pw)
    assert hashed != pw
    assert verify_password(pw, hashed) is True
    assert verify_password("wrong", hashed) is False

def test_password_hash_is_salted():
    from paperreadagent.web.auth import hash_password
    h1 = hash_password("same")
    h2 = hash_password("same")
    assert h1 != h2  # different salts

def test_cookie_sign_and_verify():
    from paperreadagent.web.auth import sign_cookie, verify_cookie
    secret = "my-secret-key"
    payload = {"user_id": 1, "exp": 9999999999}
    token = sign_cookie(payload, secret)
    assert "." in token
    decoded = verify_cookie(token, secret)
    assert decoded is not None
    assert decoded["user_id"] == 1

def test_cookie_tamper_detection():
    from paperreadagent.web.auth import sign_cookie, verify_cookie
    secret = "my-secret-key"
    token = sign_cookie({"user_id": 1, "exp": 9999999999}, secret)
    parts = token.split(".")
    parts[0] = "dGFtcGVyZWQ="  # tampered base64
    bad_token = ".".join(parts)
    assert verify_cookie(bad_token, secret) is None

def test_cookie_expired():
    import time
    from paperreadagent.web.auth import sign_cookie, verify_cookie
    secret = "my-secret-key"
    token = sign_cookie({"user_id": 1, "exp": 1}, secret)  # already expired
    assert verify_cookie(token, secret) is None

def test_login_attempt_tracking():
    from paperreadagent.web.auth import LoginGuard
    guard = LoginGuard()
    ip = "192.168.1.100"
    assert guard.is_blocked(ip) is False
    for _ in range(5):
        guard.record_failure(ip)
    assert guard.is_blocked(ip) is True

def test_login_attempt_cleanup():
    from paperreadagent.web.auth import LoginGuard
    guard = LoginGuard()
    ip = "10.0.0.1"
    # fake old failures (very old timestamp)
    guard._failures[ip] = [(100000, 1)] * 10
    assert guard.is_blocked(ip) is False  # stale entries don't count
