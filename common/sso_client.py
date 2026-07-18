"""SSO token validation for the desk — a port of the estate's Java client.

The bank (minibank) already has an SSO service that mints RS256 JWTs and
publishes its public keys at `<issuer>/.well-known/jwks.json`. This module is
the desk's half of that handshake: hand a bearer token in, get an SsoUser or
None back. It NEVER raises for a bad token — a caller must not be able to tell
"no token" from "forged token" by watching for exceptions, which is exactly
what the Java client's blanket `catch (Exception e) { return empty; }` buys.

Ported from (read these before changing anything here):
    minibank/sso-client/.../client/SsoClient.java   — the check order
    minibank/sso-client/.../client/Jwks.java        — the fetch + 300s cache
    minibank/sso-service/.../TokenIssuer.java       — the on-wire token shape

No new dependency: httpx is already core (pyproject `dependencies`), and RS256
VERIFICATION needs no crypto library at all — it is one modular exponentiation
(`pow(sig, e, n)`) plus a byte compare against the PKCS#1 v1.5 envelope. There
is no PyJWT and no `cryptography` in this venv or in the Docker image, so the
verify is hand-rolled below against hashlib/base64/int.

FOUR DELIBERATE DIVERGENCES FROM THE JAVA CLIENT (each flagged again at its
check; all four are either strictly safer or explicitly required by the desk):
  1. `alg` is enforced to be RS256. Java never reads the header alg at all and
     is protected only incidentally, by hardcoding SHA256withRSA.
  2. A string-valued `aud` is accepted (RFC 7519 allows it). Java's regex
     demands a literal '[' and REJECTS the string form.
  3. A missing `kid` is tolerated when the JWKS holds exactly one key. Java
     hard-rejects a token with no kid.
  4. Fail CLOSED on a JWKS outage: an expired cache entry is never served.
     Java re-reads its cache after a failed refresh WITHOUT re-checking expiry,
     so a stale key keeps validating tokens forever while the endpoint is down.
Everything else below is byte-for-byte the Java behaviour, including the ones
that look like bugs (zero clock skew, nbf never read, empty sub accepted).
"""

import asyncio
import base64
import hashlib
import hmac
import inspect
import json
import os
import re
import time
from typing import NamedTuple

import httpx

# The desk's own audience. The bank passes "bank.b4rruf3t.com" for the same
# token; one SSO login, two audiences, and neither service accepts the other's.
DESK_AUDIENCE = "desk.b4rruf3t.com"

# sso-service/Main.java: SSO_ISSUER, defaulting to the live host.
DEFAULT_ISSUER = "https://auth.b4rruf3t.com"

_CACHE_TTL_S = 300          # Jwks.java CACHE_TTL_SECONDS — 5 minutes, absolute
_MIN_REFRESH_INTERVAL_S = 10
_HTTP_TIMEOUT_S = 5         # Java sets NO timeout; a hung JWKS host hangs it

# DER DigestInfo prefix for SHA-256, RFC 8017 §9.2 note 1. The full PKCS#1 v1.5
# encoded message is 0x00 0x01 <0xFF...> 0x00 || this || sha256(signing input).
_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")

# Java's Base64.getUrlDecoder(): url-safe alphabet only ('+' and '/' are
# REJECTED), padding OPTIONAL. Python's base64.urlsafe_b64decode is the exact
# opposite on both counts, so the alphabet is validated by hand here.
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]*={0,2}$")


def _now() -> float:
    """Single clock seam. Everything time-dependent below goes through this,
    so a test can freeze the clock without monkeypatching time.time globally."""
    return time.time()


class SsoUser(NamedTuple):
    """Mirror of `record SsoUser(String sub, String name, String email)` —
    same three components in the same order, and nothing else is surfaced:
    no aud, exp, iat, jti, kid, no raw-claims map. name and email are
    nullable; sub is guaranteed present (though it may be the empty string)."""
    sub: str
    name: str | None
    email: str | None


class _RsaKey(NamedTuple):
    """An RSA public key as the two integers a verify actually needs."""
    n: int
    e: int


# ----------------------------------------------------------------- codecs ----

def _b64url_decode(segment: str) -> bytes:
    """Java's Base64.getUrlDecoder().decode(), reproduced exactly.

    Accepts padded and unpadded input (TokenIssuer emits unpadded via
    getUrlEncoder().withoutPadding()); rejects the standard alphabet's '+' and
    '/' the way Java does with IllegalArgumentException. Raises on bad input —
    every caller is inside validate_token's blanket except, same as Java.
    """
    if not isinstance(segment, str) or not _B64URL_RE.match(segment):
        raise ValueError("not url-safe base64")
    body = segment.rstrip("=")
    if len(body) % 4 == 1:
        raise ValueError("truncated base64")           # Java rejects this too
    return base64.b64decode(body + "=" * (-len(body) % 4),
                            altchars=b"-_", validate=True)


def _split_dots_java(token: str) -> list[str]:
    """Java's `String.split("\\.")` — which DROPS TRAILING EMPTY STRINGS.

    This is load-bearing and Python's str.split does NOT do it. "h.p." (the
    classic alg=none shape, empty signature) yields 2 parts in Java and is
    rejected by the length check; in plain Python it would yield 3 and sail
    through. The alg check below is the real defence, this is the second one.
    """
    parts = token.split(".")
    while parts and parts[-1] == "":
        parts.pop()
    return parts


# ------------------------------------------------------------- RS256 verify --

def _rs256_verify(key: _RsaKey, signing_input: bytes, signature: bytes) -> bool:
    """`Signature.getInstance("SHA256withRSA")` — RSASSA-PKCS1-v1_5 + SHA-256.

    Hardcoded, exactly as in Java: never RSA-PSS, never HMAC. Returns False on
    anything malformed rather than raising, matching sig.verify()'s false.
    """
    if key.n < 3 or key.e < 3:
        return False
    k = (key.n.bit_length() + 7) // 8
    # RFC 8017: the envelope needs at least 8 bytes of 0xFF padding.
    if k < len(_SHA256_DIGEST_INFO) + 32 + 11 or len(signature) != k:
        return False
    s = int.from_bytes(signature, "big")
    if s >= key.n:
        return False
    em = pow(s, key.e, key.n).to_bytes(k, "big")
    digest = hashlib.sha256(signing_input).digest()
    expected = (b"\x00\x01"
                + b"\xff" * (k - 3 - len(_SHA256_DIGEST_INFO) - 32)
                + b"\x00" + _SHA256_DIGEST_INFO + digest)
    return hmac.compare_digest(em, expected)


# --------------------------------------------------------------------- jwks --

class Jwks:
    """Fetches and caches RSA public keys from the SSO JWKS endpoint.

    Same 300-second absolute per-entry TTL as Jwks.java, and the same "unknown
    kid triggers a refresh" rule so a key rotation can never permanently break
    validation. Two deliberate hardenings on top of the Java version, both of
    which it is flagged for:
      * a request timeout, and a floor on how often an unknown kid can provoke
        an outbound fetch (Java has neither, making unknown-kid tokens a
        request amplifier and a thread-hang vector);
      * expired entries are never served (divergence 4 in the module docstring).
    """

    def __init__(self, url: str, ttl_s: int = _CACHE_TTL_S, transport=None):
        self.url = url
        self._ttl = ttl_s
        self._transport = transport          # httpx.MockTransport in tests
        self._keys: dict[str, tuple[_RsaKey, float]] = {}
        self._last_fetch = 0.0
        self._lock = asyncio.Lock()
        self.fetches = 0                     # observability; asserted in tests

    async def get_public_key(self, kid: str | None) -> _RsaKey | None:
        """Jwks.getPublicKey: cache, else refresh, else None. Never raises."""
        hit = self._lookup(kid)
        if hit is not None:
            return hit
        await self.refresh()
        return self._lookup(kid)

    def _lookup(self, kid: str | None) -> _RsaKey | None:
        now = _now()
        fresh = [(k, key) for k, (key, exp) in self._keys.items() if now < exp]
        if kid is None:
            # Divergence 3: Java hard-rejects a kid-less token before it ever
            # gets here. One unambiguous key means there is nothing to confuse.
            return fresh[0][1] if len(fresh) == 1 else None
        for k, key in fresh:
            if k == kid:
                return key
        return None

    async def refresh(self) -> None:
        """Re-read the JWKS. Silently gives up on any failure — Jwks.java's
        `catch (Exception e) { }` with "Silently fail — validation will return
        empty". Nothing is logged and nothing is raised at the caller."""
        async with self._lock:
            now = _now()
            if now - self._last_fetch < _MIN_REFRESH_INTERVAL_S:
                return                       # a peer just fetched; don't storm
            self._last_fetch = now
            try:
                # Async httpx, same shape as web/server.py's _mint_secret.
                async with httpx.AsyncClient(transport=self._transport,
                                             timeout=_HTTP_TIMEOUT_S) as client:
                    res = await client.get(self.url)
                self.fetches += 1
                if res.status_code != 200:   # Jwks.java: `if (!= 200) return;`
                    return
                doc = res.json()
            except Exception:
                return
            self._ingest(doc)

    def _ingest(self, doc) -> None:
        """Parse a JWKS document.

        Java splits the raw body on the literal string `{"kty":"RSA"`, so any
        pretty-printed JWKS, or one ordering kid before kty, parses to ZERO
        keys there. Real JSON here: strictly more tolerant, and it cannot
        accept a key the Java parser would reject on identity grounds, so the
        security boundary is unchanged.
        """
        keys = doc.get("keys") if isinstance(doc, dict) else None
        if not isinstance(keys, list):
            return
        expires = _now() + self._ttl
        parsed: dict[str, tuple[_RsaKey, float]] = {}
        for jwk in keys:
            if not isinstance(jwk, dict) or jwk.get("kty") != "RSA":
                continue
            kid, n, e = jwk.get("kid"), jwk.get("n"), jwk.get("e")
            # buildKey(): all three fields required, else the entry is skipped.
            if not (isinstance(kid, str) and isinstance(n, str) and isinstance(e, str)):
                continue
            try:
                # `new BigInteger(1, bytes)` — UNSIGNED big-endian. KeyManager
                # emits BigInteger.toByteArray(), which carries a leading 0x00
                # sign byte; int.from_bytes(..., "big") tolerates it the same.
                key = _RsaKey(int.from_bytes(_b64url_decode(n), "big"),
                              int.from_bytes(_b64url_decode(e), "big"))
            except Exception:
                continue                     # buildKey returns null -> skipped
            if key.n < 3 or key.e < 3:
                continue
            parsed[kid] = (key, expires)
        if parsed:
            # Java only ever ADDS keys, so a rotated-out key stays usable. A
            # successful fetch is authoritative here: rotate out, drop it.
            self._keys = parsed


# --------------------------------------------------------------------- core --

class SsoClient:
    """Validates JWTs issued by the b4rruf3t SSO service."""

    def __init__(self, issuer: str, key_resolver=None, *,
                 jwks_url: str | None = None, transport=None):
        """Production: `SsoClient(issuer)` resolves keys over HTTP.
        Test/advanced: pass key_resolver, a (kid|None) -> _RsaKey|None callable
        (sync or async), mirroring the Java `Function<String, RSAPublicKey>`
        constructor."""
        self.issuer = issuer
        self.jwks: Jwks | None = None
        if key_resolver is None:
            # SsoClient.java line 21: naive concatenation, no trailing-slash
            # stripping, no openid-configuration discovery.
            self.jwks = Jwks(jwks_url or issuer + "/.well-known/jwks.json",
                             transport=transport)
            key_resolver = self.jwks.get_public_key
        self._resolve = key_resolver

    async def validate_token(self, token: str,
                             expected_audience: str | None = DESK_AUDIENCE
                             ) -> SsoUser | None:
        """Validate a JWT. The user if valid, None otherwise — and None for
        EVERY failure mode, with no reason code, no logging and no exception,
        which is the whole point of the Java `catch (Exception e)` wrapper.
        Malformed base64, bad UTF-8, a dead JWKS host: all land here as None."""
        try:
            return await self._validate(token, expected_audience)
        except Exception:
            return None

    async def _validate(self, token: str,
                        expected_audience: str | None) -> SsoUser | None:
        # 1. STRUCTURE — `if (parts.length != 3) return empty;` over a split
        #    that drops trailing empties (see _split_dots_java).
        parts = _split_dots_java(token)
        if len(parts) != 3:
            return None

        # 2. HEADER. Java pulls "kid" with a regex over the raw text; real JSON
        #    parsing here, which additionally rejects a header that is not a
        #    JSON object at all (Java would shrug and read on). Fail closed.
        header = json.loads(_b64url_decode(parts[0]))
        if not isinstance(header, dict):
            return None

        # 2a. ALG — divergence 1. Java NEVER reads the header alg: no
        #     allowlist, no rejection of "none" or "HS256" per se, contained
        #     only because verification is hardcoded to SHA256withRSA. The
        #     issuer emits {"alg":"RS256"} on every token, so demanding it
        #     cannot break a real token, and it closes alg-confusion by intent
        #     rather than by accident.
        if header.get("alg") != "RS256":
            return None

        # 2b. KID — divergence 3. Java: `if (kid == null) return empty;`. Here
        #     a kid-less token is still resolvable, but ONLY when the JWKS
        #     holds exactly one key (the resolver enforces that, above).
        kid = header.get("kid")
        if kid is not None and not isinstance(kid, str):
            return None

        # 3. KEY LOOKUP — `if (publicKey == null) return empty;`. An unknown
        #    kid costs one JWKS refresh inside the resolver, so a rotation
        #    heals itself instead of locking everyone out.
        key = self._resolve(kid)
        if inspect.isawaitable(key):        # resolver may be sync or async
            key = await key
        if key is None:
            return None

        # 4. SIGNATURE — over the ORIGINAL received base64url text of the first
        #    two segments joined by a literal ".", encoded UTF-8. The segments
        #    are never re-encoded, so canonicalisation differences are moot.
        #    Verified BEFORE any payload claim is read: the order is deliberate.
        if not _rs256_verify(key, (parts[0] + "." + parts[1]).encode("utf-8"),
                             _b64url_decode(parts[2])):
            return None

        payload = json.loads(_b64url_decode(parts[1]))
        if not isinstance(payload, dict):
            return None

        # 5. EXP — MANDATORY, and the comparison is strict `>` on integer epoch
        #    seconds against Instant.now(): CLOCK SKEW TOLERANCE IS ZERO, and
        #    exp == now is still VALID. Java's `(\d+)` regex means a
        #    non-integer or negative exp reads as missing, i.e. reject.
        #    NOT CHECKED, exactly as in Java: nbf (a not-yet-valid token is
        #    ACCEPTED), iat, jti, typ, token revocation, maximum age.
        exp = payload.get("exp")
        if not isinstance(exp, int) or isinstance(exp, bool) or exp < 0:
            return None
        if int(_now()) > exp:
            return None

        # 6. ISS — `if (!issuer.equals(iss)) return empty;`. Exact,
        #    case-sensitive string equality against the configured issuer. No
        #    trailing-slash normalisation, no URL parsing. Missing iss rejects.
        if payload.get("iss") != self.issuer:
            return None

        # 7. AUD — skipped ENTIRELY when expected_audience is None, same as
        #    Java's `if (expectedAudience != null && ...)`.
        if expected_audience is not None and not _has_audience(payload, expected_audience):
            return None

        # 8. SUB — presence only. An empty-string sub ("sub":"") is non-null in
        #    Java and is ACCEPTED as an identity; kept for parity. Callers
        #    resolving `identity.sub or fallback` are unaffected either way.
        sub = payload.get("sub")
        if not isinstance(sub, str):
            return None

        # 9. NAME/EMAIL — both optional, never validated. Java extracts these
        #    with a first-match regex over the raw payload text and performs NO
        #    JSON unescaping, so a display name containing a quote comes back
        #    truncated there and a name claim can shadow a later email claim.
        #    Real parsing here returns the true values; that is a bug fix, not
        #    a boundary change, since neither field is used for authorisation.
        name = payload.get("name")
        email = payload.get("email")
        return SsoUser(sub,
                       name if isinstance(name, str) else None,
                       email if isinstance(email, str) else None)


def _has_audience(payload: dict, expected: str) -> bool:
    """SsoClient.hasAudience, widened by divergence 2.

    Java matches `"aud"\\s*:\\s*\\[([^\\]]*)\\]` over the raw payload text: it
    requires a literal '[', so an RFC 7519 string-valued aud is REJECTED there.
    The desk accepts both forms. Element comparison is exact string equality,
    the honest version of Java's trim-then-strip-every-quote on raw text.
    """
    aud = payload.get("aud")
    if isinstance(aud, str):
        return aud == expected                     # the widened branch
    if isinstance(aud, list):
        return any(a == expected for a in aud)
    return False


# ------------------------------------------------------------- desk wiring ---

_default: SsoClient | None = None


def issuer() -> str:
    """SSO_ISSUER, same env var and same default as the bank's sso-service."""
    return os.getenv("SSO_ISSUER", DEFAULT_ISSUER)


def default_client() -> SsoClient:
    """Process-wide client, so the 300s JWKS cache is actually shared."""
    global _default
    if _default is None or _default.issuer != issuer():
        _default = SsoClient(issuer())
    return _default


async def validate(token: str,
                   audience: str | None = DESK_AUDIENCE) -> SsoUser | None:
    return await default_client().validate_token(token, audience)


async def validate_bearer(header_value: str | None,
                          audience: str | None = DESK_AUDIENCE) -> SsoUser | None:
    """Validate an Authorization header. Mirrors BankAuth: the prefix must be
    exactly "Bearer " (case-sensitive) and the token is everything after it.
    None for a missing header, so an unauthenticated request stays anonymous
    rather than becoming an error."""
    if not isinstance(header_value, str) or not header_value.startswith("Bearer "):
        return None
    return await validate(header_value[7:], audience)
