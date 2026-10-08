"""Verification primitives for the two directions Vercel talks to us in.

Inbound, Vercel sends **two different kinds of credential** and conflating them
is the classic way an integration server ends up trusting an attacker:

* Partner API calls carry a **JWT** signed by Vercel's marketplace key. It is
  RS256, published as a JWKS at ``marketplace.vercel.com``, and the whole point
  is that we verify the signature against a key we fetched from Vercel -- not
  against anything the caller supplied.
* Webhooks carry an **HMAC-SHA1** of the raw body, keyed with the integration
  client secret, in ``x-vercel-signature``.

They share nothing but this module, and the checks never fall through to "ok".

Why RS256 is verified here instead of by a dependency
-----------------------------------------------------

The obvious answer is PyJWT. It is not in ``requirements.txt`` for a reason this
repository keeps relearning: a serverless Python function has a hard size
budget, and ``cryptography`` is a large native wheel whose version has to agree
with the image's OpenSSL. More importantly, a hand-rolled verifier is only safe
when it is *small and exhaustively tested*, which is why this file has no key
generation, no signing, no encryption and no algorithm negotiation -- it
implements exactly one path, RS256 with PKCS#1 v1.5, and every branch that is
not that path raises. The test vectors in ``tests/test_marketplace_crypto.py``
are real 2048-bit keys produced by OpenSSL rather than mocks, so the arithmetic
is exercised against a genuine signature.

The rules that make the shortcut defensible:

* ``alg`` must be the literal ``RS256``. ``none``, ``HS256`` and the SHA-384/512
  variants are refused before any key is touched, which is what stops the
  algorithm-confusion attack where an attacker signs with the public key as if
  it were an HMAC secret.
* The modulus must be at least ``MIN_RSA_BITS``. A short key is how a forged
  JWKS slips past.
* The padded block is compared whole, with ``compare_digest`` -- not by
  searching for the digest inside it, which is what makes Bleichenbacher's
  forgery work.
* ``exp`` is mandatory. A token that never expires is a token that outlives the
  permission it was issued for.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import string
import time
from typing import Callable, Mapping, Optional, Sequence

from .errors import MarketplaceError

# --- limits ---------------------------------------------------------------

MIN_RSA_BITS = 2048
MAX_TOKEN_BYTES = 16 * 1024
MAX_JWKS_BYTES = 512 * 1024
MAX_KEYS_PER_JWKS = 64
# A little slack for clock drift between us and Vercel. Seconds, and small: a
# large skew is how an expired token keeps working.
DEFAULT_LEEWAY_SECONDS = 60
# Vercel's RS256 keys rotate; refetching on every request would add a round trip
# to every dashboard click, and never refetching would break the day they roll.
JWKS_TTL_SECONDS = 3600
# But a signature made with a kid we have never seen is exactly the rotation
# signal, so that case refetches immediately -- bounded so a flood of bogus kids
# cannot turn into a flood of outbound requests.
JWKS_MIN_REFRESH_SECONDS = 10

_B64URL_ALPHABET = frozenset(string.ascii_letters + string.digits + "-_")

# DER prefix for `DigestInfo ::= SEQUENCE { SEQUENCE { OID 2.16.840.1.101.3.4.2.1,
# NULL }, OCTET STRING }` with SHA-256: 0x30 0x31 ... 0x04 0x20. Nineteen bytes,
# then the 32-byte digest.
SHA256_DIGEST_INFO_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def _b64url_decode(segment: str) -> bytes:
    """Decode one base64url segment, refusing everything else.

    Rejecting ``+``/``/`` and embedded padding matters more than it looks: two
    different byte strings that decode to the same value would let a token be
    modified after it was signed while still passing a "canonical form" check.
    """
    if not segment or not set(segment) <= _B64URL_ALPHABET:
        raise MarketplaceError("مقطع التوقيع غير صالح.", code="malformed_token",
                               status=403)
    if len(segment) % 4 == 1:
        raise MarketplaceError("مقطع التوقيع غير صالح.", code="malformed_token",
                               status=403)
    try:
        return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    except (binascii.Error, ValueError) as error:
        raise MarketplaceError("مقطع التوقيع غير صالح.", code="malformed_token",
                               status=403) from error


# --- RSA PKCS#1 v1.5 signature verification --------------------------------


class RsaPublicKey:
    """An RSA public key parsed from a JWK. Nothing else, and it cannot sign."""

    __slots__ = ("kid", "modulus", "exponent", "size_bytes")

    def __init__(self, kid: str, modulus: int, exponent: int):
        self.kid = kid
        self.modulus = modulus
        self.exponent = exponent
        self.size_bytes = (modulus.bit_length() + 7) // 8

    @classmethod
    def from_jwk(cls, jwk: Mapping) -> "RsaPublicKey":
        """Parse one JWK, refusing anything that is not a signing RSA key.

        Every rejection here is a key that would otherwise be trusted. ``use``
        is checked because a JWKS may legitimately publish an encryption key
        next to the signing key, and ``alg`` because a key pinned to ``RS512``
        must not be accepted for an ``RS256`` token.
        """
        if not isinstance(jwk, Mapping):
            raise MarketplaceError("مفتاح غير صالح.", code="malformed_jwk", status=403)
        if jwk.get("kty") != "RSA":
            raise MarketplaceError("نوع المفتاح غير مدعوم.", code="unsupported_key_type",
                                   status=403)
        if jwk.get("use") not in (None, "sig"):
            raise MarketplaceError("المفتاح ليس للتوقيع.", code="unsupported_key_use",
                                   status=403)
        if jwk.get("alg") not in (None, "RS256"):
            raise MarketplaceError("خوارزمية المفتاح غير مدعومة.",
                                   code="unsupported_key_algorithm", status=403)
        raw_n = jwk.get("n")
        raw_e = jwk.get("e")
        if not isinstance(raw_n, str) or not isinstance(raw_e, str):
            raise MarketplaceError("المفتاح ناقص.", code="malformed_jwk", status=403)
        try:
            modulus = int.from_bytes(_b64url_decode(raw_n), "big")
            exponent = int.from_bytes(_b64url_decode(raw_e), "big")
        except MarketplaceError as error:
            raise MarketplaceError("المفتاح غير صالح.", code="malformed_jwk",
                                   status=403) from error
        bits = modulus.bit_length()
        if bits < MIN_RSA_BITS:
            raise MarketplaceError("المفتاح أقصر من الحد الأدنى.", code="key_too_small",
                                   status=403)
        # e is a public exponent: even or tiny values are the signature of a
        # malformed (or deliberately weak) key, not of a real Vercel key.
        if exponent < 65537 or exponent % 2 == 0 or exponent >= modulus:
            raise MarketplaceError("أُسّ المفتاح غير مقبول.", code="unsupported_exponent",
                                   status=403)
        kid = jwk.get("kid")
        return cls(str(kid) if kid is not None else "", modulus, exponent)


def verify_rs256(signing_input: bytes, signature: bytes, key: RsaPublicKey) -> bool:
    """Check one PKCS#1 v1.5 SHA-256 signature. Returns a bool, never raises.

    The recovered block is rebuilt and compared in full rather than inspected
    for a digest: a partial check is precisely the forgery margin this encoding
    has historically leaked.
    """
    if not signature or len(signature) != key.size_bytes:
        return False
    digest_info = SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(signing_input).digest()
    block_length = key.size_bytes
    if block_length < len(digest_info) + 11:
        return False
    padding_length = block_length - len(digest_info) - 3
    expected = b"\x00\x01" + b"\xff" * padding_length + b"\x00" + digest_info
    try:
        recovered = pow(int.from_bytes(signature, "big"), key.exponent, key.modulus)
    except ValueError:
        return False
    try:
        actual = recovered.to_bytes(block_length, "big")
    except OverflowError:
        return False
    return hmac.compare_digest(actual, expected)


# --- JWT -------------------------------------------------------------------


def _decode_json_segment(segment: str, code: str) -> dict:
    raw = _b64url_decode(segment)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as error:
        raise MarketplaceError("محتوى التوكن غير صالح.", code=code, status=403) from error
    if not isinstance(value, dict):
        raise MarketplaceError("محتوى التوكن غير صالح.", code=code, status=403)
    return value


def decode_token_parts(token: str):
    """Split a compact JWS into ``(header, claims, signing_input, signature_segment)``.

    Only structural checks happen here -- no key, no clock, and crucially *no
    signature decoding*. The signature segment is returned undecoded so that the
    caller can reject the token's algorithm first: ``alg: none`` ships an empty
    signature, and decoding it before looking at ``alg`` would report that as a
    malformed token instead of as the bypass it is an attempt at.
    """
    if not isinstance(token, str) or not token or len(token.encode("utf-8")) > MAX_TOKEN_BYTES:
        raise MarketplaceError("التوكن مفقود أو كبير جداً.", code="malformed_token",
                               status=403)
    parts = token.split(".")
    if len(parts) != 3:
        raise MarketplaceError("بنية التوكن غير صحيحة.", code="malformed_token", status=403)
    header_segment, claims_segment, signature_segment = parts
    header = _decode_json_segment(header_segment, "malformed_token")
    signing_input = f"{header_segment}.{claims_segment}".encode("ascii")
    return header, _decode_json_segment(claims_segment, "malformed_token"), \
        signing_input, signature_segment


def verify_jwt(token: str, keys: Sequence[RsaPublicKey], *, issuer: str, audience: str,
               now: Optional[float] = None,
               leeway: int = DEFAULT_LEEWAY_SECONDS) -> dict:
    """Verify a Vercel marketplace JWT and return its claims.

    ``keys`` is the *fetched* key set -- never a key taken from the token
    header. ``aud`` must equal our integration client id: without that check a
    token minted for a different integration would be accepted here.
    """
    if not issuer or not audience:
        raise MarketplaceError("لم يتم تهيئة مُصدر التوكن أو جمهوره.",
                               code="not_configured", status=503)
    header, claims, signing_input, signature_segment = decode_token_parts(token)
    # The algorithm is settled before the signature is even looked at. Decoding
    # first would let `alg: none` -- whose signature is empty by definition --
    # surface as a formatting problem rather than as a rejected algorithm.
    algorithm = header.get("alg")
    if algorithm != "RS256":
        raise MarketplaceError("خوارزمية التوكن غير مدعومة.",
                               code="unsupported_algorithm", status=403)
    # `crit` asks us to understand headers we do not implement; honouring it
    # silently is how an extension becomes a downgrade.
    if header.get("crit") is not None:
        raise MarketplaceError("حقول رأس التوكن غير مدعومة.",
                               code="unsupported_header", status=403)
    signature = _b64url_decode(signature_segment)
    if not signature:
        raise MarketplaceError("التوقيع فارغ.", code="malformed_token", status=403)
    kid = header.get("kid")
    candidates = [key for key in keys if key.kid and kid and key.kid == kid]
    if not candidates and not kid:
        candidates = list(keys)
    if not candidates:
        raise MarketplaceError("مفتاح التوقيع غير معروف.", code="unknown_key", status=403)
    if not any(verify_rs256(signing_input, signature, key) for key in candidates):
        raise MarketplaceError("توقيع التوكن غير صالح.", code="bad_signature", status=403)

    if claims.get("iss") != issuer:
        raise MarketplaceError("مُصدر التوكن غير موثوق.", code="bad_issuer", status=403)
    aud = claims.get("aud")
    aud_matches = aud == audience or (isinstance(aud, list) and audience in aud)
    if not aud_matches:
        raise MarketplaceError("جمهور التوكن غير مطابق.", code="bad_audience", status=403)

    clock = time.time() if now is None else now
    expires = claims.get("exp")
    if not isinstance(expires, (int, float)):
        raise MarketplaceError("التوكن بلا تاريخ انتهاء.", code="claims_missing",
                               status=403)
    if clock - leeway > float(expires):
        raise MarketplaceError("انتهت صلاحية التوكن.", code="token_expired", status=403)
    not_before = claims.get("nbf")
    if isinstance(not_before, (int, float)) and clock + leeway < float(not_before):
        raise MarketplaceError("التوكن غير ساري بعد.", code="token_not_yet_valid",
                               status=403)
    return claims


# --- JWKS ------------------------------------------------------------------


class JwksCache:
    """Fetch Vercel's JWKS and cache it for one hour.

    Two failure modes are kept apart on purpose. A **network** failure returns
    the last good key set when there is one (Vercel's keys are long-lived, so a
    stale set beats a dashboard outage), and raises when there is none -- a
    first-boot failure must look like a failure. A **parse** failure discards the
    cache and raises, because a JWKS we cannot read is not a JWKS we should keep
    trusting.
    """

    def __init__(self, url: str, fetcher: Callable[[str], bytes], ttl: int = JWKS_TTL_SECONDS,
                 clock=time.time, min_refresh: int = JWKS_MIN_REFRESH_SECONDS):
        self.url = url
        self.fetcher = fetcher
        self.ttl = ttl
        self.clock = clock
        self.min_refresh = min_refresh
        self._keys: list = []
        self._fetched_at = 0.0

    def keys(self, force: bool = False) -> list:
        now = self.clock()
        fresh = self._keys and now - self._fetched_at < self.ttl
        if fresh and not force and now - self._fetched_at >= 0:
            return list(self._keys)
        if force and now - self._fetched_at < self.min_refresh and self._keys:
            return list(self._keys)
        try:
            payload = self.fetcher(self.url)
        except MarketplaceError:
            if self._keys:
                return list(self._keys)
            raise
        if not isinstance(payload, (bytes, str)):
            raise MarketplaceError("استجابة JWKS غير صالحة.", code="bad_upstream_payload",
                                   status=502)
        raw = payload.encode("utf-8") if isinstance(payload, str) else payload
        if len(raw) > MAX_JWKS_BYTES:
            raise MarketplaceError("استجابة JWKS كبيرة جداً.", code="bad_upstream_payload",
                                   status=502)
        try:
            document = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as error:
            self._keys, self._fetched_at = [], 0.0
            raise MarketplaceError("استجابة JWKS ليست JSON.", code="bad_upstream_payload",
                                   status=502) from error
        listed = document.get("keys") if isinstance(document, dict) else None
        if not isinstance(listed, list) or not listed:
            self._keys, self._fetched_at = [], 0.0
            raise MarketplaceError("استجابة JWKS بلا مفاتيح.", code="bad_upstream_payload",
                                   status=502)
        keys = []
        for entry in listed[:MAX_KEYS_PER_JWKS]:
            try:
                keys.append(RsaPublicKey.from_jwk(entry))
            except MarketplaceError:
                # One unusable entry is normal during a rotation; skipping it is
                # safe precisely because the *good* keys are what get used.
                continue
        if not keys:
            self._keys, self._fetched_at = [], 0.0
            raise MarketplaceError("لا مفتاح صالح في JWKS.", code="bad_upstream_payload",
                                   status=502)
        self._keys, self._fetched_at = keys, now
        return list(keys)

    def invalidate(self):
        self._keys, self._fetched_at = [], 0.0


# --- webhook signature -----------------------------------------------------


def webhook_signature(body: bytes, secret: str) -> str:
    """Vercel signs webhook bodies with HMAC-SHA1 keyed by the client secret.

    SHA-1 is Vercel's choice, not ours: this is a MAC over a body we already
    received, so the collision property SHA-1 lost does not apply. What matters
    is that the comparison is constant-time.
    """
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha1).hexdigest()


def verify_webhook(body: bytes, header: Optional[str], secret: str) -> bool:
    if not secret or not header:
        return False
    return hmac.compare_digest(webhook_signature(body, secret), str(header).strip())


# --- signed values (install state + dashboard session) ---------------------

# Prefixed so a value minted for one purpose cannot be replayed as another: the
# install `state` and the dashboard session are both HMAC'd with the same key.
STATE_PREFIX = "waha-mp-state"
SESSION_PREFIX = "waha-mp-session"


def sign_value(prefix: str, payload: str, secret: str, issued: Optional[int] = None,
               clock=time.time) -> str:
    stamped = int(clock()) if issued is None else int(issued)
    material = f"{prefix}|{payload}|{stamped}"
    signature = hmac.new(secret.encode("utf-8"), material.encode("utf-8"),
                         hashlib.sha256).hexdigest()
    return f"{stamped}.{signature}"


def verify_signed_value(prefix: str, payload: str, token: Optional[str], secret: str,
                        ttl_seconds: int, now: Optional[float] = None,
                        clock=time.time) -> bool:
    """Constant-time check of a value we signed, with an expiry."""
    if not secret or not token or not isinstance(token, str):
        return False
    parts = token.split(".")
    if len(parts) != 2 or not parts[0].isdigit():
        return False
    stamped = int(parts[0])
    clock_now = time.time() if now is None else now
    if stamped > clock_now + 60 or stamped < clock_now - ttl_seconds:
        return False
    expected = sign_value(prefix, payload, secret, issued=stamped, clock=clock)
    return hmac.compare_digest(token, expected)
