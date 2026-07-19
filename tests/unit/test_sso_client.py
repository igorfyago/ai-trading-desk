"""The desk's SSO validator: what it accepts, and everything it must refuse.

Tokens here are minted in-process. There is no PyJWT and no `cryptography` in
this venv (nor in the Docker image, which installs only [web,postgres]), so the
test rig hand-rolls RSA keygen and RS256 SIGNING the same way common/sso_client
hand-rolls verification · and the same way the bank's TokenIssuer.java does it,
byte for byte, right down to the unpadded base64url segments and the JWKS's
leading BigInteger sign byte.

The port's four deliberate divergences from the Java client (alg enforcement,
string-form aud, kid-less single-key JWKS, fail-closed on a JWKS outage) each
get a test that pins the DESK's behaviour and names what Java does instead.
"""

import asyncio
import base64
import hashlib
import json
import random
import time

import httpx
import pytest

from common import sso_client
from common.sso_client import Jwks, SsoClient, SsoUser

ISSUER = "https://auth.b4rruf3t.com"
AUDIENCE = "desk.b4rruf3t.com"
KID = "sso-1"

_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")
_rng = random.Random(20260718)      # deterministic keys: reproducible failures


# --------------------------------------------------------------- the rig -----

def _is_prime(n: int) -> bool:
    for p in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d, r = d // 2, r + 1
    for a in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = x * x % n
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits: int) -> int:
    while True:
        c = _rng.getrandbits(bits) | (1 << (bits - 1)) | 1
        if _is_prime(c):
            return c


def _keypair(bits: int = 1024):
    """(n, e, d). 1024 bits: RSA verification is size-agnostic and the suite
    should not spend seconds hunting 1024-bit primes for a unit test."""
    e = 65537
    while True:
        p, q = _prime(bits // 2), _prime(bits // 2)
        if p == q:
            continue
        lam = (p - 1) * (q - 1) // __import__("math").gcd(p - 1, q - 1)
        if lam % e == 0:
            continue
        n = p * q
        if n.bit_length() != bits:
            continue
        return n, e, pow(e, -1, lam)


KEY_N, KEY_E, KEY_D = _keypair()
OTHER_N, OTHER_E, OTHER_D = _keypair()


def _b64u(raw: bytes) -> str:
    """TokenIssuer: Base64.getUrlEncoder().withoutPadding()."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _sign(data: bytes, n: int = KEY_N, d: int = KEY_D) -> bytes:
    """RSASSA-PKCS1-v1_5 over SHA-256 · the signing half of what the module
    verifies. Same envelope: 0x00 0x01 <0xFF...> 0x00 || DigestInfo || hash."""
    k = (n.bit_length() + 7) // 8
    digest = hashlib.sha256(data).digest()
    em = (b"\x00\x01" + b"\xff" * (k - 3 - len(_SHA256_DIGEST_INFO) - 32)
          + b"\x00" + _SHA256_DIGEST_INFO + digest)
    return pow(int.from_bytes(em, "big"), d, n).to_bytes(k, "big")


_DEFAULT = object()     # so a test can mint an explicit null aud/exp


def mint(*, kid=KID, alg="RS256", iss=ISSUER, sub="usr_igor", aud=_DEFAULT,
         exp=_DEFAULT, name="Igor Yago", email="igor@b4rruf3t.com",
         drop=(), sign_with=None, tamper=False, blank_sig=False) -> str:
    """Build a token in TokenIssuer.java's exact shape and field order."""
    header = {"alg": alg, "typ": "JWT", "kid": kid}
    now = int(time.time())
    payload = {
        "iss": iss,
        "sub": sub,
        "aud": [AUDIENCE] if aud is _DEFAULT else aud,
        "exp": now + 900 if exp is _DEFAULT else exp,
        "iat": now,
        "jti": "tok_test",
        "name": name,
        "email": email,
    }
    for field in drop:
        header.pop(field, None)
        payload.pop(field, None)
    h = _b64u(json.dumps(header, separators=(",", ":")).encode())
    p = _b64u(json.dumps(payload, separators=(",", ":")).encode())
    if blank_sig:
        return f"{h}.{p}."
    n, d = sign_with or (KEY_N, KEY_D)
    sig = bytearray(_sign(f"{h}.{p}".encode("utf-8"), n, d))
    if tamper:
        sig[-1] ^= 0xFF
    return f"{h}.{p}.{_b64u(bytes(sig))}"


def _java_bigint_bytes(i: int) -> bytes:
    """BigInteger.toByteArray(): minimal SIGNED big-endian, so a modulus with
    its top bit set carries a leading 0x00. KeyManager.toJwksJson base64s
    exactly this, and the validator must read it back unsigned."""
    return i.to_bytes(i.bit_length() // 8 + 1, "big")


def jwks_body(*entries) -> str:
    """KeyManager.toJwksJson's shape: one {kty,use,kid,n,e,alg} per key."""
    keys = [{"kty": "RSA", "use": "sig", "kid": kid,
             "n": _b64u(_java_bigint_bytes(n)),
             "e": _b64u(_java_bigint_bytes(e)), "alg": "RS256"}
            for kid, n, e in entries]
    return json.dumps({"keys": keys})


def client(*, issuer=ISSUER, keys=None) -> SsoClient:
    """A client wired to a fixed in-memory key, no HTTP (the Java client's
    `SsoClient(issuer, keyResolver)` constructor)."""
    table = keys if keys is not None else {KID: sso_client._RsaKey(KEY_N, KEY_E)}
    return SsoClient(issuer, lambda kid: table.get(kid))


def validate(token, aud=AUDIENCE, **kw) -> SsoUser | None:
    return asyncio.run(client(**kw).validate_token(token, aud))


# --------------------------------------------------------- the happy path ----

def test_valid_token_returns_the_user():
    user = validate(mint())
    assert user == SsoUser("usr_igor", "Igor Yago", "igor@b4rruf3t.com")
    assert (user.sub, user.name, user.email) == user   # record component order


def test_name_and_email_are_optional():
    user = validate(mint(drop=("name", "email")))
    assert user == SsoUser("usr_igor", None, None)


def test_exp_exactly_now_is_still_valid():
    """Java compares with a strict `>` against Instant.now().getEpochSecond(),
    so a token expiring this very second is accepted. Zero skew, both ways."""
    assert validate(mint(exp=int(time.time()))) is not None


def test_no_clock_skew_grace():
    """One second past exp is dead · Java grants no leeway at all."""
    assert validate(mint(exp=int(time.time()) - 1)) is None


def test_nbf_in_the_future_is_ignored():
    """Parity gotcha: the Java client never reads nbf (or iat), so a
    not-yet-valid token IS accepted. Pinned so a future 'fix' is a choice."""
    token = mint()
    assert validate(token) is not None
    header, payload, sig = token.split(".")
    assert "nbf" not in base64.urlsafe_b64decode(
        payload + "=" * (-len(payload) % 4)).decode()


def test_an_empty_sub_is_not_an_identity():
    """This used to assert the opposite, pinned as deliberate parity with Java,
    whose `if (sub == null)` was a PRESENCE check that let "sub":"" through.
    Java now rejects it, so the parity argument that justified accepting it has
    gone, and what is left is the reason it was always wrong: downstream code
    writes `identity.sub or session`, so an empty sub silently degrades an
    authenticated caller to anonymous. That is the quietest possible way to
    lose an authorisation."""
    assert validate(mint(sub="")) is None


def test_a_duplicated_claim_is_refused_by_both_clients():
    """The divergence that mattered most. json.loads keeps the LAST duplicate,
    the Java client's regex kept the FIRST, so one signed token authenticated
    two different people depending on which service received it. Neither client
    picks a winner now: both refuse, which is the only answer they can agree on
    without coordinating a tie-break rule."""
    import base64, json as _json

    token = mint()
    head, payload_b64, sig = token.split(".")
    payload = _json.loads(base64.urlsafe_b64decode(payload_b64 + "=="))
    raw = _json.dumps(payload)
    # a second "sub" appended by hand, exactly what a hostile issuer would emit
    doubled = raw[:-1] + ', "sub": "usr_admin"}'
    tampered = base64.urlsafe_b64encode(doubled.encode()).decode().rstrip("=")

    # the signature no longer matches, so prove the REFUSAL is the parser's by
    # checking the parser directly as well
    from common import sso_client
    with pytest.raises(ValueError):
        sso_client._strict_json(doubled.encode())
    assert validate(f"{head}.{tampered}.{sig}") is None


def test_unpadded_and_padded_base64_both_decode():
    """TokenIssuer emits unpadded segments; Java's url decoder takes either.
    Python's urlsafe_b64decode would choke on the unpadded form.

    Only the signature segment can be re-padded after the fact: the signature
    covers the ORIGINAL header.payload TEXT, so padding those two changes the
    signed bytes and correctly stops verifying (Java behaves identically)."""
    h, p, s = mint().split(".")
    assert validate(f"{h}.{p}.{s}") is not None
    assert validate(f"{h}.{p}.{s + '=' * (-len(s) % 4)}") is not None
    repadded = ".".join(x + "=" * (-len(x) % 4) for x in (h, p))
    assert validate(f"{repadded}.{s}") is None


# ------------------------------------------------------------- rejections ----

def test_expired_token_is_rejected():
    assert validate(mint(exp=int(time.time()) - 60)) is None


def test_missing_exp_is_rejected():
    """exp is MANDATORY · a token without one is not 'never expires'."""
    assert validate(mint(drop=("exp",))) is None


@pytest.mark.parametrize("exp", ["1900000000", -1, None, 1.5e9, True])
def test_non_integer_exp_is_rejected(exp):
    """Java's `(\\d+)` regex only matches unsigned digits; anything else reads
    as a missing exp, which rejects."""
    assert validate(mint(exp=exp)) is None


def test_wrong_audience_is_rejected():
    assert validate(mint(aud=["bank.b4rruf3t.com"])) is None


def test_audience_is_not_a_prefix_or_substring_match():
    assert validate(mint(aud=["desk.b4rruf3t.com.evil.test"])) is None
    assert validate(mint(aud=["desk"])) is None


def test_empty_null_or_missing_audience_is_rejected():
    assert validate(mint(aud=[])) is None
    assert validate(mint(aud=None)) is None          # "aud": null
    assert validate(mint(drop=("aud",))) is None


def test_missing_sub_is_rejected():
    assert validate(mint(drop=("sub",))) is None


def test_wrong_issuer_is_rejected():
    """Exact, case-sensitive equality · no URL parsing, no trailing-slash
    normalisation, which is why each of these fails."""
    assert validate(mint(iss="https://evil.test")) is None
    assert validate(mint(iss=ISSUER + "/")) is None
    assert validate(mint(iss=ISSUER.upper())) is None
    assert validate(mint(drop=("iss",))) is None


def test_bad_signature_is_rejected():
    assert validate(mint(tamper=True)) is None


def test_signature_from_a_different_key_is_rejected():
    assert validate(mint(sign_with=(OTHER_N, OTHER_D))) is None


def test_payload_tampering_breaks_the_signature():
    """The signature covers the ORIGINAL header.payload text, so swapping in a
    re-encoded payload with a fatter sub invalidates it."""
    h, _, s = mint().split(".")
    forged = _b64u(json.dumps({
        "iss": ISSUER, "sub": "usr_admin", "aud": [AUDIENCE],
        "exp": int(time.time()) + 900, "name": "x", "email": "x@x.test",
    }, separators=(",", ":")).encode())
    assert validate(f"{h}.{forged}.{s}") is None


def test_unknown_kid_is_rejected_after_a_refetch_attempt():
    """The resolver gets one shot at the unknown kid (that is what makes a key
    rotation survivable); when it still comes back empty, the token dies."""
    seen = []

    def resolver(kid):
        seen.append(kid)
        return None

    got = asyncio.run(SsoClient(ISSUER, resolver).validate_token(mint(kid="sso-99"), AUDIENCE))
    assert got is None
    assert seen == ["sso-99"]           # asked, and asked for the RIGHT kid


@pytest.mark.parametrize("token", [
    "",                                 # nothing
    "garbage",
    "a.b",                              # two segments
    "a.b.c.d",                          # four
    "....",
    "not a jwt at all",
    "eyJhbGciOiJSUzI1NiJ9",             # header only
])
def test_malformed_tokens_return_none_without_raising(token):
    assert validate(token) is None


@pytest.mark.parametrize("token", [None, b"bytes.are.not.str", 42, []])
def test_non_string_input_returns_none_without_raising(token):
    """The blanket `catch (Exception e)` means even a caller passing junk gets
    a clean None rather than a 500 out of the handler."""
    assert validate(token) is None


def test_non_url_safe_base64_is_rejected():
    """Java's url decoder REJECTS '+' and '/'; Python's silently accepts them.
    The port validates the alphabet by hand to keep the boundary identical."""
    h, p, s = mint().split(".")
    assert validate(f"{h}.{p}.{s[:-4] + 'a+/b'}") is None
    assert validate(f"{h[:-4] + 'a+/b'}.{p}.{s}") is None


def test_header_or_payload_that_is_not_json_is_rejected():
    h, p, s = mint().split(".")
    assert validate(f"{_b64u(b'not json')}.{p}.{s}") is None
    assert validate(f"{h}.{_b64u(b'[]')}.{s}") is None


def test_empty_segments_are_rejected():
    _, p, s = mint().split(".")
    assert validate(f".{p}.{s}") is None


# ------------------------------------------------------- alg (divergence 1) --

def test_alg_none_is_rejected():
    """Two independent defences. Java has only the second one, by accident."""
    assert validate(mint(alg="none", blank_sig=True)) is None


def test_alg_none_with_a_real_signature_is_still_rejected():
    """The alg allowlist, on its own: this token's RS256 signature is genuine
    and would verify · Java (which never reads alg) would ACCEPT it."""
    assert validate(mint(alg="none")) is None


@pytest.mark.parametrize("alg", ["HS256", "RS512", "PS256", "rs256", "", None])
def test_only_rs256_is_accepted(alg):
    assert validate(mint(alg=alg)) is None


def test_trailing_empty_segment_is_dropped_like_java_split():
    """"h.p." is 2 parts in Java and 3 in plain Python. The port reproduces
    Java's trailing-empty drop, so the classic alg=none shape fails the
    structure check before anything else even looks at it."""
    h, p, _ = mint().split(".")
    assert sso_client._split_dots_java(f"{h}.{p}.") == [h, p]
    assert validate(f"{h}.{p}.") is None


# ------------------------------------------------------- aud (divergence 2) --

def test_string_audience_is_accepted():
    """RFC 7519 allows a bare string aud. The Java regex demands a literal '['
    and REJECTS it; the desk accepts both forms, as required."""
    assert validate(mint(aud=AUDIENCE)) is not None
    assert validate(mint(aud="bank.b4rruf3t.com")) is None


def test_multi_audience_array_hits_the_desk_entry():
    """One SSO login, several apps: the bank's audience riding along is fine."""
    assert validate(mint(aud=["bank.b4rruf3t.com", AUDIENCE])) is not None


def test_audience_check_is_skipped_when_expected_is_none():
    """Java: `if (expectedAudience != null && ...)`. Passing None means the
    caller is opting out of the check entirely."""
    assert validate(mint(aud=["somewhere.else"]), aud=None) is not None


def test_default_audience_is_the_desk():
    assert sso_client.DESK_AUDIENCE == "desk.b4rruf3t.com"
    assert asyncio.run(client().validate_token(mint())) is not None
    assert asyncio.run(client().validate_token(mint(aud=["bank.b4rruf3t.com"]))) is None


# ------------------------------------------------------- kid (divergence 3) --

def test_missing_kid_resolves_against_a_single_key_jwks():
    """Java hard-rejects a kid-less token. With exactly one key published
    there is nothing to disambiguate, so the desk lets it through."""
    jwks = Jwks("https://auth.test/jwks", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text=jwks_body((KID, KEY_N, KEY_E)))))
    got = asyncio.run(SsoClient(ISSUER, jwks.get_public_key)
                      .validate_token(mint(drop=("kid",)), AUDIENCE))
    assert got is not None


def test_missing_kid_is_rejected_when_the_jwks_has_several_keys():
    jwks = Jwks("https://auth.test/jwks", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text=jwks_body((KID, KEY_N, KEY_E),
                                                     ("sso-2", OTHER_N, OTHER_E)))))
    got = asyncio.run(SsoClient(ISSUER, jwks.get_public_key)
                      .validate_token(mint(drop=("kid",)), AUDIENCE))
    assert got is None


def test_non_string_kid_is_rejected():
    assert validate(mint(kid=1)) is None


# ------------------------------------------------------------ jwks + cache ---

def _serving(body_for, status=200):
    """A MockTransport plus a per-call counter."""
    calls = []

    def handler(request):
        calls.append(str(request.url))
        body, code = body_for(len(calls))
        return httpx.Response(code if code else status, text=body)

    return httpx.MockTransport(handler), calls


def test_jwks_is_fetched_once_and_cached():
    transport, calls = _serving(lambda n: (jwks_body((KID, KEY_N, KEY_E)), 200))
    c = SsoClient(ISSUER, transport=transport)
    for _ in range(3):
        assert asyncio.run(c.validate_token(mint(), AUDIENCE)) is not None
    assert len(calls) == 1              # 300s TTL: one fetch covers all three


def test_jwks_url_is_the_issuer_plus_well_known():
    """SsoClient.java line 21 · naive concatenation, no discovery document."""
    assert SsoClient(ISSUER).jwks.url == ISSUER + "/.well-known/jwks.json"


def test_key_rotation_is_picked_up_on_an_unknown_kid():
    """The whole reason an unknown kid forces a refetch: the SSO service
    rotates its keypair on restart, and the desk must heal without a deploy."""
    rotated = {"yet": False}

    def body(_n):
        if rotated["yet"]:
            return jwks_body(("sso-2", OTHER_N, OTHER_E)), 200
        return jwks_body((KID, KEY_N, KEY_E)), 200

    transport, calls = _serving(body)
    c = SsoClient(ISSUER, transport=transport)
    assert asyncio.run(c.validate_token(mint(), AUDIENCE)) is not None

    rotated["yet"] = True
    new_token = mint(kid="sso-2", sign_with=(OTHER_N, OTHER_D))
    c.jwks._last_fetch = 0.0            # skip the anti-storm floor, not the TTL
    assert asyncio.run(c.validate_token(new_token, AUDIENCE)) is not None
    assert len(calls) == 2
    # and the rotated-OUT key stops working, unlike Java's add-only cache
    c.jwks._last_fetch = 0.0
    assert asyncio.run(c.validate_token(mint(), AUDIENCE)) is None


def test_jwks_fetch_failure_yields_no_identity_not_an_exception():
    transport, calls = _serving(lambda n: ("", 503))
    c = SsoClient(ISSUER, transport=transport)
    assert asyncio.run(c.validate_token(mint(), AUDIENCE)) is None
    assert len(calls) == 1


def test_jwks_transport_error_yields_no_identity():
    def boom(request):
        raise httpx.ConnectError("sso is down", request=request)

    c = SsoClient(ISSUER, transport=httpx.MockTransport(boom))
    assert asyncio.run(c.validate_token(mint(), AUDIENCE)) is None


def test_garbage_jwks_body_yields_no_identity():
    transport, _ = _serving(lambda n: ("<html>502 bad gateway</html>", 200))
    c = SsoClient(ISSUER, transport=transport)
    assert asyncio.run(c.validate_token(mint(), AUDIENCE)) is None


def test_expired_cache_plus_a_dead_endpoint_fails_closed():
    """Divergence 4, and the sharpest one. Jwks.java re-reads its cache after a
    failed refresh WITHOUT re-checking expiry, so a stale key keeps validating
    tokens for as long as the JWKS host stays down. The desk refuses."""
    def body(n):
        return (jwks_body((KID, KEY_N, KEY_E)), 200) if n == 1 else ("", 500)

    transport, calls = _serving(body)
    c = SsoClient(ISSUER, transport=transport)
    c.jwks._ttl = 0.1
    assert asyncio.run(c.validate_token(mint(), AUDIENCE)) is not None

    time.sleep(0.15)                    # cache entry now past its TTL
    c.jwks._last_fetch = 0.0
    assert asyncio.run(c.validate_token(mint(), AUDIENCE)) is None
    assert len(calls) == 2              # it did try to refresh first


def test_unknown_kid_does_not_hammer_the_jwks_endpoint():
    """Java refetches on EVERY unknown-kid token with no backoff, which makes
    a junk-kid flood an outbound request amplifier. A 10s floor still lets a
    rotation heal, since the refetch just lands on the next attempt."""
    transport, calls = _serving(lambda n: (jwks_body((KID, KEY_N, KEY_E)), 200))
    c = SsoClient(ISSUER, transport=transport)
    for _ in range(20):
        assert asyncio.run(c.validate_token(mint(kid="junk"), AUDIENCE)) is None
    assert len(calls) == 1


def test_jwks_leading_sign_byte_is_read_unsigned():
    """KeyManager base64s BigInteger.toByteArray(), so the modulus arrives with
    a leading 0x00. `new BigInteger(1, bytes)` ignores it and so must we -
    read it signed and every signature check would fail."""
    raw = _java_bigint_bytes(KEY_N)
    assert raw[0] == 0 and len(raw) == KEY_N.bit_length() // 8 + 1
    jwks = Jwks("https://auth.test/jwks", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text=jwks_body((KID, KEY_N, KEY_E)))))
    assert asyncio.run(jwks.get_public_key(KID)) == sso_client._RsaKey(KEY_N, KEY_E)


def test_pretty_printed_jwks_still_parses():
    """Java splits the raw body on the literal `{"kty":"RSA"` and would parse
    ZERO keys out of this. Real JSON parsing here · strictly more tolerant,
    and it cannot admit a key Java would have rejected on identity grounds."""
    pretty = json.dumps(json.loads(jwks_body((KID, KEY_N, KEY_E))), indent=2)
    jwks = Jwks("https://auth.test/jwks", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text=pretty)))
    assert asyncio.run(jwks.get_public_key(KID)) is not None


def test_non_rsa_jwks_entries_are_skipped():
    doc = json.dumps({"keys": [
        {"kty": "EC", "kid": KID, "x": "aaaa", "y": "bbbb"},
        {"kty": "RSA", "kid": "no-modulus"},
    ]})
    jwks = Jwks("https://auth.test/jwks", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text=doc)))
    assert asyncio.run(jwks.get_public_key(KID)) is None


# -------------------------------------------------- the golden vector --------

# A REAL token, minted by the bank's own dev/b4rruf3t/sso/TokenIssuer.java on
# Java 21 against a real KeyManager keypair, paired with that KeyManager's own
# toJwksJson() output. Everything above this line is Python talking to Python;
# this is the only test that proves the port agrees with the Java issuer about
# the wire format · the unpadded segments, the field order, the SHA256withRSA
# signature, and the leading 0x00 sign byte BigInteger.toByteArray() puts on
# the modulus (visible as the "AN..." prefix on n).
#
# Regenerate: javac the two sso-service classes, issueAccessToken(...), paste.

GOLDEN_JWKS = (
    '{"keys":[{"kty":"RSA","use":"sig","kid":"sso-1784406840421","n":'
    '"ANq8v-fxMWna_VGInl7HfY8oDKvl9CW2C1FXtc33UhivRLKk-E-x8eGx1KNvLLK'
    'GY5QTR-sRSoya_QD1-GDlCu2E96IifQ7z13zn1Eh7rXrtU0dU3R5-HlkH3_xxxxN'
    'RROJOlzpeI-7lZ3Ebdux7cACUpFbZoRA6Gxiy6QtW98vvKtxak3iJx_LfJxgBDtl'
    'l-Hvk8HG8FxT0ZIVVE7RO50tBF5etqervFnjKl6NYDzxsdCCT0Kh0SheouP7PEVu'
    'CMwbpzv4w6ljYiAwNFZka_b5lS2qSksT2UKJq_UpEPtnDu4GkY6v1ySeXhfPbmpX'
    'J5OOyl3NvDcEYptPhV6ayE48","e":"AQAB","alg":"RS256"}]}'
)

# iss=https://auth.b4rruf3t.com, sub=usr_igor, iat=1784406840, exp=1784407740,
# aud=["bank.b4rruf3t.com","desk.b4rruf3t.com"] · one login, both apps.
GOLDEN_TOKEN = (
    "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCIsImtpZCI6InNzby0xNzg0NDA2ODQw"
    "NDIxIn0.eyJpc3MiOiJodHRwczovL2F1dGguYjRycnVmM3QuY29tIiwic3ViIjoi"
    "dXNyX2lnb3IiLCJhdWQiOlsiYmFuay5iNHJydWYzdC5jb20iLCJkZXNrLmI0cnJ1"
    "ZjN0LmNvbSJdLCJleHAiOjE3ODQ0MDc3NDAsImlhdCI6MTc4NDQwNjg0MCwianRp"
    "IjoidG9rXzE5Zjc2ZWY5NDc2MjZjMTNlNWIiLCJuYW1lIjoiSWdvciBZYWdvIiwi"
    "ZW1haWwiOiJpZ29yQGI0cnJ1ZjN0LmNvbSJ9.wyoZwu9qWuaWwsb8shIQtMzDepl"
    "C7apU3bVfe19xhwYTmyD77pMjvIDX5KgOZhq2_9ZLyDe3kOwIt5mLozywi7pCjaa"
    "ryQeRLltTA-T65_hWlFKs-jT2AtNM4fI1KtTm90kzoD8yIrTiwUQEEEVNTV5SilJ"
    "14UaQV1WYXEhl6kNc5_JvxiLWw_Vxs1je5noN36AvE4kolNLdSDt3hWz3moxtwpr"
    "3dgRyO7Mz5EWhDqmVhturHLZ-g5naQ6tHv7PIzxN5PNVYJ0R8ik0kuLYuvr-8roJ"
    "PW_pkbhPyc3XQAzndZ6GNySDjbDNMqYTyXKhNB2LHkLLdrT32VXhzsO-zHA"
)
GOLDEN_EXP = 1784407740


@pytest.fixture()
def frozen_clock(monkeypatch):
    """Pin the module's clock seam inside the token's live window, so the
    vector never rots. Patching sso_client._now rather than time.time keeps
    the freeze off httpx's and asyncio's own timekeeping."""
    monkeypatch.setattr(sso_client, "_now", lambda: float(GOLDEN_EXP - 300))


def _golden_client() -> SsoClient:
    return SsoClient(ISSUER, transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text=GOLDEN_JWKS)))


def test_a_real_java_issued_token_validates(frozen_clock):
    user = asyncio.run(_golden_client().validate_token(GOLDEN_TOKEN, AUDIENCE))
    assert user == SsoUser("usr_igor", "Igor Yago", "igor@b4rruf3t.com")


def test_the_real_token_also_carries_the_banks_audience(frozen_clock):
    """The same token is what BankAuth validates with "bank.b4rruf3t.com" -
    proof the desk isn't quietly accepting a token minted for somewhere else."""
    c = _golden_client()
    assert asyncio.run(c.validate_token(GOLDEN_TOKEN, "bank.b4rruf3t.com")) is not None
    assert asyncio.run(c.validate_token(GOLDEN_TOKEN, "minipay.b4rruf3t.com")) is None


def test_the_real_token_expires(monkeypatch):
    monkeypatch.setattr(sso_client, "_now", lambda: float(GOLDEN_EXP + 1))
    assert asyncio.run(_golden_client().validate_token(GOLDEN_TOKEN, AUDIENCE)) is None


def test_the_real_tokens_expiry_boundary_is_exact(monkeypatch):
    """The whole zero-skew rule, on a real token: valid at exp, dead at exp+1."""
    monkeypatch.setattr(sso_client, "_now", lambda: float(GOLDEN_EXP))
    assert asyncio.run(_golden_client().validate_token(GOLDEN_TOKEN, AUDIENCE)) is not None


def test_the_real_token_does_not_survive_tampering(frozen_clock):
    """Tamper the DECODED signature bytes, not the base64 text: the last
    character of an unpadded 256-byte signature encodes only 2 significant
    bits, so flipping it is a no-op that would fake a passing test."""
    h, p, s = GOLDEN_TOKEN.split(".")
    raw = bytearray(base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)))
    assert len(raw) == 256                       # 2048-bit key, full block
    raw[0] ^= 0x01
    flipped = _b64u(bytes(raw))
    assert flipped != s
    assert asyncio.run(_golden_client().validate_token(f"{h}.{p}.{flipped}", AUDIENCE)) is None


def test_the_real_tokens_claims_cannot_be_swapped(frozen_clock):
    """Re-encoding the payload with a different sub invalidates it, because
    the signature covers the received header.payload text verbatim."""
    h, p, s = GOLDEN_TOKEN.split(".")
    claims = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    assert claims["sub"] == "usr_igor"           # the real issuer's own shape
    claims["sub"] = "usr_admin"
    forged = _b64u(json.dumps(claims, separators=(",", ":")).encode())
    assert asyncio.run(_golden_client().validate_token(f"{h}.{forged}.{s}", AUDIENCE)) is None


# ------------------------------------------------------------ desk wiring ----

def test_issuer_comes_from_env_with_the_banks_default(monkeypatch):
    monkeypatch.delenv("SSO_ISSUER", raising=False)
    assert sso_client.issuer() == "https://auth.b4rruf3t.com"
    monkeypatch.setenv("SSO_ISSUER", "https://auth.local.test")
    assert sso_client.issuer() == "https://auth.local.test"
    # the cached process-wide client must follow the env, not outlive it
    assert sso_client.default_client().issuer == "https://auth.local.test"


def test_validate_bearer_requires_the_exact_prefix(monkeypatch):
    """BankAuth checks for "Bearer " then takes substring(7). Same here · and
    a missing header is None, not an error, so anonymous stays anonymous."""
    monkeypatch.setattr(sso_client, "_default", client())
    monkeypatch.setattr(sso_client, "issuer", lambda: ISSUER)
    token = mint()
    assert asyncio.run(sso_client.validate_bearer(f"Bearer {token}")) is not None
    for header in [None, "", token, f"bearer {token}", f"Bearer{token}",
                   f"Token {token}", "Bearer ", "Bearer  " + token]:
        assert asyncio.run(sso_client.validate_bearer(header)) is None
