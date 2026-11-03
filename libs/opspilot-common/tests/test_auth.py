import pytest
from opspilot_common.auth import InvalidTokenError, Principal, Role, TokenIssuer, TokenVerifier, can_decide
from opspilot_common.events import Gate

SECRET = "test-secret-that-is-long-enough-for-hs256"


def test_round_trip() -> None:
    p = Principal("u1", "Alex", frozenset({Role.SUPPORT_AGENT}), mfa=True)
    assert TokenVerifier(SECRET).verify(TokenIssuer(SECRET).issue(p)) == p


def test_rejects_tampered_or_foreign_tokens() -> None:
    token = TokenIssuer("another-secret-that-is-also-long-enough").issue(Principal("u1", "Alex"))
    with pytest.raises(InvalidTokenError):
        TokenVerifier(SECRET).verify(token)


def test_only_support_agents_approve_production_fixes() -> None:
    assert can_decide({Role.SUPPORT_AGENT}, Gate.FIX)
    assert not can_decide({Role.L1_ANALYST}, Gate.FIX)
    assert not can_decide({Role.ADMIN}, Gate.FIX)
    assert can_decide({Role.L1_ANALYST}, Gate.TICKET)
    assert not can_decide({Role.COMPLIANCE_LEAD}, Gate.MESSAGE)


def test_service_token_names_the_caller_and_checks_allow_list() -> None:
    from opspilot_common.auth import ServiceTokenIssuer, ServiceTokenVerifier

    issuer = ServiceTokenIssuer("s" * 32, "mail-service")
    verifier = ServiceTokenVerifier("s" * 32)
    assert verifier.verify(issuer.token(), {"mail-service"}) == "mail-service"
    assert issuer.token() == issuer.token()  # cached until shortly before expiry
    with pytest.raises(InvalidTokenError):
        verifier.verify(issuer.token(), {"jira-connector"})
    with pytest.raises(InvalidTokenError):
        ServiceTokenVerifier("t" * 32).verify(issuer.token())


def test_session_and_service_tokens_are_not_interchangeable() -> None:
    from opspilot_common.auth import ServiceTokenIssuer, ServiceTokenVerifier

    secret = "s" * 32
    session = TokenIssuer(secret).issue(Principal("u1", "Lena", frozenset({Role.ADMIN})))
    with pytest.raises(InvalidTokenError):
        ServiceTokenVerifier(secret).verify(session)
    with pytest.raises(InvalidTokenError):
        TokenVerifier(secret).verify(ServiceTokenIssuer(secret, "mail-service").token())
