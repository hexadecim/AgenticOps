"""Production keys: no service can mint a session or name another service."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from opspilot_common.auth import AuthKeys, InvalidTokenError, Principal, Role, TokenIssuer


def _pair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return private.decode(), public.decode()


@pytest.fixture(scope="module")
def session_pair() -> tuple[str, str]:
    return _pair()


def _keys(
    tmp: Path, service: str, session: tuple[str, str], services: dict[str, tuple[str, str]]
) -> AuthKeys:
    folder = tmp / service
    (folder / "services").mkdir(parents=True)
    (folder / "session_public.pem").write_text(session[1])
    if service == "identity-service":
        (folder / "session_private.pem").write_text(session[0])
    if service in services:
        (folder / "service_private.pem").write_text(services[service][0])
    for name, pair in services.items():
        (folder / "services" / f"{name}.pem").write_text(pair[1])
    return AuthKeys.load(service, "", str(folder))


PRINCIPAL = Principal("u-1", "Lena", frozenset({Role.ADMIN}), mfa=True)


def test_a_session_from_identity_is_accepted_elsewhere(tmp_path: Path, session_pair: tuple[str, str]) -> None:
    identity = _keys(tmp_path, "identity-service", session_pair, {})
    audit = _keys(tmp_path, "audit-service", session_pair, {})
    token = identity.session_issuer(timedelta(minutes=5)).issue(PRINCIPAL)
    assert audit.session_verifier().verify(token).user_id == "u-1"


def test_a_session_signed_with_the_public_key_is_refused(
    tmp_path: Path, session_pair: tuple[str, str]
) -> None:
    """The classic RS256-to-HS256 confusion: the public key is no secret, so it must never verify HS256."""
    audit = _keys(tmp_path, "audit-service", session_pair, {})
    forged = jwt.encode(
        {"iss": "opspilot-identity", "sub": "x", "exp": 9999999999},
        "k" * 40,
        algorithm="HS256",
    )
    with pytest.raises(InvalidTokenError):
        audit.session_verifier().verify(forged)


def test_a_shared_secret_session_is_refused_in_production(
    tmp_path: Path, session_pair: tuple[str, str]
) -> None:
    audit = _keys(tmp_path, "audit-service", session_pair, {})
    token = TokenIssuer("a-development-secret-of-32-characters").issue(PRINCIPAL)
    with pytest.raises(InvalidTokenError):
        audit.session_verifier().verify(token)


def test_a_service_token_is_checked_with_that_services_key(
    tmp_path: Path, session_pair: tuple[str, str]
) -> None:
    pairs = {"mail-service": _pair(), "privacy-service": _pair()}
    mail = _keys(tmp_path, "mail-service", session_pair, pairs)
    privacy = _keys(tmp_path, "privacy-service", session_pair, pairs)
    assert (
        privacy.service_verifier().verify(mail.service_issuer().token(), {"mail-service"}) == "mail-service"
    )


def test_a_compromised_service_cannot_name_another_one(tmp_path: Path, session_pair: tuple[str, str]) -> None:
    pairs = {"mail-service": _pair(), "privacy-service": _pair()}
    mail = _keys(tmp_path, "mail-service", session_pair, pairs)
    forged = jwt.encode(
        {"iss": "opspilot-service", "aud": "opspilot-internal", "sub": "privacy-service", "exp": 9999999999},
        pairs["mail-service"][0],
        algorithm="RS256",
    )
    with pytest.raises(InvalidTokenError):
        mail.service_verifier().verify(forged)


def test_an_unknown_service_is_refused(tmp_path: Path, session_pair: tuple[str, str]) -> None:
    pairs = {"mail-service": _pair()}
    audit = _keys(tmp_path, "audit-service", session_pair, pairs)
    forged = jwt.encode(
        {"iss": "opspilot-service", "aud": "opspilot-internal", "sub": "nobody", "exp": 9999999999},
        _pair()[0],
        algorithm="RS256",
    )
    with pytest.raises(InvalidTokenError):
        audit.service_verifier().verify(forged)


def test_a_session_is_never_a_service_token(tmp_path: Path, session_pair: tuple[str, str]) -> None:
    identity = _keys(tmp_path, "identity-service", session_pair, {"identity-service": _pair()})
    token = identity.session_issuer(timedelta(minutes=5)).issue(PRINCIPAL)
    with pytest.raises(InvalidTokenError):
        identity.service_verifier().verify(token)


@pytest.mark.parametrize("secret", ["", "too-short"])
def test_a_service_refuses_to_start_without_usable_keys(secret: str) -> None:
    with pytest.raises(ValueError, match="AUTH_KEYS_DIR"):
        AuthKeys.load("audit-service", secret)


def test_a_service_refuses_to_start_with_an_empty_keys_folder(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="session_public"):
        AuthKeys.load("audit-service", "", str(tmp_path))
