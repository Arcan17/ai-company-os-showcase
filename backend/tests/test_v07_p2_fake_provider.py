"""V0.7 P2 (Issue #23) — el proveedor SIMULADO es ESTRICTO. Si el simulado aceptara cualquier cosa, todas las demás pruebas de P2 serían una demostración falsa: aquí se comprueba que
se comporta como un proveedor OIDC con PKCE (código de un solo uso y de vida corta, verificador S256, nonce, desactivación, caída) y que su interfaz es la estrecha del diseño.
Escritas EN ROJO antes de implementar. Sin red. LO QUE NO DEMUESTRAN: que WorkOS se comporte así (se comprueba en P3)."""
import base64
import hashlib
import inspect

import pytest

from .idp_util import FrozenClock, fake_provider, query

NARROW = ["authorize_url", "disable", "exchange", "invite", "logout_url"]


def pkce(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


@pytest.fixture
def prov(monkeypatch):
    FrozenClock().install(monkeypatch)
    return fake_provider()


def begin(prov, subject, *, verifier="v" * 43, nonce="n" * 22, state="s" * 43, **kw):
    url = prov.authorize_url(state, nonce, pkce(verifier), login_hint=None)
    return prov.login(url, subject, **kw)["code"]


def test_the_interface_is_the_narrow_one_of_the_design():
    from app import identity
    public = sorted(n for n, _ in inspect.getmembers(identity.IdentityProvider, inspect.isfunction) if not n.startswith("_"))
    assert public == NARROW
    fake = type(fake_provider())
    assert all(callable(getattr(fake, n)) for n in NARROW) and fake.is_fake is True and fake.name == "fake"


def test_authorize_url_never_points_to_a_real_host(prov):
    url = prov.authorize_url("s" * 43, "n" * 22, pkce("v" * 43), login_hint="ana@alfa.test", force_reauth=True)
    assert url.startswith("https://") and ".invalid/" in url
    assert query(url)["login_hint"] == "ana@alfa.test" and query(url)["prompt"] == "login" and query(url)["code_challenge_method"] == "S256"


def test_exchange_returns_the_identity_when_the_pkce_verifier_matches(prov):
    sub = prov.account("ana@alfa.test", name="Ana")
    code = begin(prov, sub, verifier="a" * 43, nonce="N" * 22)
    ident = prov.exchange(code, "a" * 43, nonce="N" * 22)
    assert (ident.provider, ident.subject, ident.email, ident.email_verified, ident.name) == ("fake", sub, "ana@alfa.test", True, "Ana") and isinstance(ident.auth_time, int)


@pytest.mark.parametrize("what", ["wrong_verifier", "wrong_nonce", "unknown_code", "empty_code"])
def test_exchange_refuses_a_wrong_verifier_nonce_or_code(prov, what):
    from app.identity import IdentityError
    sub = prov.account("ana@alfa.test")
    code = begin(prov, sub, verifier="a" * 43, nonce="N" * 22)
    args = {"wrong_verifier": (code, "b" * 43, "N" * 22), "wrong_nonce": (code, "a" * 43, "M" * 22), "unknown_code": ("zzz", "a" * 43, "N" * 22), "empty_code": ("", "a" * 43, "N" * 22)}[what]
    with pytest.raises(IdentityError):
        prov.exchange(args[0], args[1], nonce=args[2])


def test_a_code_works_once_and_a_failed_attempt_burns_it(prov):
    from app.identity import IdentityError
    sub = prov.account("ana@alfa.test")
    code = begin(prov, sub, verifier="a" * 43)
    assert prov.exchange(code, "a" * 43).subject == sub
    with pytest.raises(IdentityError):
        prov.exchange(code, "a" * 43)
    code2 = begin(prov, sub, verifier="a" * 43)
    with pytest.raises(IdentityError):
        prov.exchange(code2, "x" * 43)
    with pytest.raises(IdentityError):
        prov.exchange(code2, "a" * 43)                                                           # el intento fallido quemó el código (RFC 6749 §4.1.2)


def test_a_code_expires_after_sixty_seconds_of_the_controlled_clock(monkeypatch):
    from app import clock
    from app.identity import IdentityError
    clk = FrozenClock().install(monkeypatch)
    prov = fake_provider()
    sub = prov.account("ana@alfa.test")
    ok = begin(prov, sub, verifier="a" * 43)
    clk.advance(seconds=59)
    assert prov.exchange(ok, "a" * 43).subject == sub
    late = begin(prov, sub, verifier="a" * 43)
    clk.advance(seconds=61)
    with pytest.raises(IdentityError):
        prov.exchange(late, "a" * 43)


def test_down_and_disabled_behave_like_a_failing_and_a_revoking_provider(prov):
    from app.identity import IdentityError, ProviderUnavailable
    sub = prov.account("ana@alfa.test")
    code = begin(prov, sub, verifier="a" * 43)
    prov.down = True
    with pytest.raises(ProviderUnavailable):
        prov.exchange(code, "a" * 43)
    prov.down = False
    prov.disable(sub)
    assert prov.disabled == {sub}
    with pytest.raises(IdentityError):
        prov.exchange(begin(prov, sub, verifier="a" * 43), "a" * 43)


def test_invite_only_records_the_address_in_memory(prov):
    prov.invite("Ana@Alfa.test")
    assert prov.outbox == ["ana@alfa.test"] and not hasattr(prov, "send")


def test_an_unverified_account_is_reported_as_such(prov):
    sub = prov.account("ana@alfa.test", email_verified=False)
    assert prov.exchange(begin(prov, sub, verifier="a" * 43), "a" * 43).email_verified is False


def test_the_identity_value_object_is_immutable():
    from app.identity import Identity
    ident = Identity(provider="fake", subject="s", email="a@b.test", email_verified=True, name=None, auth_time=1)
    with pytest.raises(Exception):
        ident.subject = "otro"                                                                    # type: ignore[misc]
