"""The verification primitives, exercised against real RSA keys.

The keys below were generated with OpenSSL and are *test* keys: they are
committed deliberately, because a signature verifier that has only ever been
checked against a signature made by the same code has not been checked at all.
Signing here uses the private exponent from a different tool than the one that
performs the verification, so an error in the padding, the digest encoding or
the byte order shows up as a failure instead of as two bugs cancelling out.

Every refusal is asserted on its ``code``. The messages are Arabic and will be
reworded; the codes are the contract.
"""
import base64
import hashlib
import hmac
import json
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from marketplace import crypto                    # noqa: E402
from marketplace.errors import MarketplaceError   # noqa: E402

# --- test vectors: 2048-bit keys produced by `openssl genrsa` ---------------

TEST_N = int(
    "b71e88882f60773a1ef7f018e285521488BD5602DF44FEEBD7944085073807A0431C1EEE29FAE9C754060"
    "37B5DDE2554E46E1BD337A595E06ADD9F4F059699FDEC060788DF64AFCC1C2DAD4D2E55E2885313E337B3C"
    "0AF0A8AFB0D0804873B31BDCF47D66278283833BF830F527B88988E2124A0EAB65E578B8C87DA83EB8F16D"
    "CBD5A7661202C8372712637DA2D4C9159EA4C526DB06E506B50B0F3C63189DBC5A726A7955EE65CB6A775E"
    "76F92EE2A842837C0F4C000011E5775E3A1D4B27B39F9C9E6B4F9608F2F2037F9EECE3F3BF201DE101672D"
    "14BE50DCBBB8E6019B8DED4B9ECADA2E24847E118AD27C60BF6D32DBA1F05D5EF7150B2CA516D6EE335", 16)
TEST_D = int(
    "11f83aaf4f56e275ec0c109e06efccba3587588bd13521b22b199544df0c7aecb32939afcd706ac55720a"
    "ff1fc0e8ca9b0ca808bb73b0962f2001f2acf9bfa2efccff7755170573337a0ad7694ab0db8ab01996821e"
    "f9770f89daf0daff7454bcd1a2c93512b37cd928009706eaee24fdc8f7f417b46eef65055d14247749a39e"
    "6edc6441f275bd70c1dcd9d3af00782b5419dada11dd412f401de6a5a1d5b790597c4e5ea5367a4099b537"
    "f7a66de0e930b11de784e2414b50a09b8a4a0b859fc990c973ff42316c2e884d13fb29e99af2e464b1a45f"
    "1f4916cec441b0c0a46b434f2578f1ef47cc1cf4314bdfb5d52c4f05cec35bc4166cb97427172dd169", 16)

OTHER_N = int(
    "aa32f4de044d0db24b98257c29ccee2374090525474c924bbe2d0c7718c81ee6465feb08c916103660311"
    "83cf3be9601408673db32c8ad29aee3565d5352004887474a22e2e4f1b3c4f0bafadbccbeb3b8e76b6cb91"
    "a068e52d5c55228228acac66b0b968811549430b18e6f6331388f5ad1393a318911a8c6899477283f732a7"
    "73f7e4c56013750ab4ec2be02c8b9a16e5e55eaa0de66d1a37c02a3352816700a09a7e3d55f4003bf87e3d"
    "d6dec51e26a77ab8cbbe5fcd889bb600b21c5493e6b196facff3df92db8bb22a4c5ca000205820f921ae8f"
    "da917c25ea2a60815040eaca7cf7541b203711e2d6fce62df4d84ece4c22858832fa86f4167e6a948d5", 16)
OTHER_D = int(
    "660d27c5f7ce3be82c6b8e0e6501fbc8464b224ab7f5b1c2b8c54a341f27df5f33d34f9a74f845c054ed1"
    "b1b710b55e66a4a342dbac5990ee54e6afa8a7b20b05c6ce28708853e680c2e4bef1edc257c9de225078bf"
    "58ef8c7b2661e5cf259463cb2f2bfcfd6946046d2301599e469858b6ee8617c5233c47f46ff3e73639829"
    "62a169edb19986a135391c9cb48ea6a1c4a767a5e039e211a94b139910148530fcbbe36ec5a3f2582b75e4"
    "7cf112c0fd164b1f01923790bd3d400b03a6feae13db3ef5b2a7bf59a0d9853685f4ebe8dbe8cd16539467"
    "2c35073f9018f9e5211a793d70783e2c4aa2c80101ca002d16756effc726da14ff1180d16322be8363", 16)

PUBLIC_EXPONENT = 65537
KID = "test-key-1"
ISSUER = "https://marketplace.vercel.com"
AUDIENCE = "oac_testClientId"


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _int_to_b64(value: int) -> str:
    return _b64u(value.to_bytes((value.bit_length() + 7) // 8, "big"))


def jwk(n: int = TEST_N, e: int = PUBLIC_EXPONENT, kid: str = KID, **extra) -> dict:
    entry = {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": kid,
             "n": _int_to_b64(n), "e": _int_to_b64(e)}
    entry.update(extra)
    return entry


def sign_rs256(message: bytes, n: int = TEST_N, d: int = TEST_D) -> bytes:
    """PKCS#1 v1.5 signature with the private exponent, as OpenSSL would."""
    digest_info = crypto.SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(message).digest()
    size = (n.bit_length() + 7) // 8
    padding = size - len(digest_info) - 3
    if padding < 8:
        raise AssertionError("test key too small for the fixture")
    block = b"\x00\x01" + b"\xff" * padding + b"\x00" + digest_info
    signature = pow(int.from_bytes(block, "big"), d, n)
    return signature.to_bytes(size, "big")


def make_jwt(claims=None, *, header=None, n=TEST_N, d=TEST_D, kid=KID,
             bad_signature=False):
    head = {"alg": "RS256", "typ": "JWT"}
    if kid is not None:
        head["kid"] = kid
    if header:
        head.update(header)
    body = {"iss": ISSUER, "aud": AUDIENCE, "exp": int(time.time()) + 300,
            "iat": int(time.time()), "installation_id": "icfg_test", "user_role": "ADMIN",
            "account_id": "team_test", "user_id": "user_test", "sub": "account:x:user:y"}
    body.update(claims or {})
    head_segment = _b64u(json.dumps(head, separators=(",", ":")).encode())
    claims_segment = _b64u(json.dumps(body, separators=(",", ":")).encode())
    signing_input = f"{head_segment}.{claims_segment}".encode("ascii")
    signature = sign_rs256(signing_input, n, d)
    if bad_signature:
        signature = bytes([signature[0] ^ 0x01]) + signature[1:]
    return f"{head_segment}.{claims_segment}.{_b64u(signature)}"


def test_keys(*entries):
    return [crypto.RsaPublicKey.from_jwk(entry) for entry in entries]


KEYS = test_keys(jwk())


def verify(token, keys=None, now=None, issuer=ISSUER, audience=AUDIENCE):
    return crypto.verify_jwt(token, KEYS if keys is None else keys, issuer=issuer,
                             audience=audience, now=now)


def assert_code(test_case, code, callable_, *args, **kwargs):
    """Run and require one specific refusal. Asserts the code, never the prose."""
    with test_case.assertRaises(MarketplaceError) as caught:
        callable_(*args, **kwargs)
    test_case.assertEqual(caught.exception.code, code)
    return caught.exception


class RsaVerification(unittest.TestCase):
    def test_a_genuine_signature_verifies(self):
        message = b"header.claims"
        self.assertTrue(crypto.verify_rs256(message, sign_rs256(message), KEYS[0]))

    def test_a_tampered_payload_does_not(self):
        self.assertFalse(crypto.verify_rs256(b"header.claims", sign_rs256(b"other.claims"),
                                             KEYS[0]))

    def test_a_signature_from_another_key_does_not(self):
        """The whole point of fetching Vercel's JWKS instead of trusting headers."""
        message = b"header.claims"
        self.assertFalse(crypto.verify_rs256(message, sign_rs256(message, OTHER_N, OTHER_D),
                                             KEYS[0]))

    def test_a_short_or_empty_signature_is_refused(self):
        message = b"header.claims"
        signature = sign_rs256(message)
        self.assertFalse(crypto.verify_rs256(message, signature[:-1], KEYS[0]))
        self.assertFalse(crypto.verify_rs256(message, signature + b"\x00", KEYS[0]))
        self.assertFalse(crypto.verify_rs256(message, b"", KEYS[0]))

    def test_a_forgery_that_only_embeds_the_digest_is_refused(self):
        """Bleichenbacher's shape: garbage around a correct digest must fail.

        A verifier that searched for the digest inside the recovered block would
        accept this. Comparing the padded block in full is what rejects it.
        """
        message = b"header.claims"
        digest = hashlib.sha256(message).digest()
        size = KEYS[0].size_bytes
        forged = pow(int.from_bytes(b"\x00\x01" + b"\x00" * (size - 35) + digest, "big"),
                     TEST_D, TEST_N).to_bytes(size, "big")
        self.assertFalse(crypto.verify_rs256(message, b"\x00" + forged[1:], KEYS[0]))


class JwkParsing(unittest.TestCase):
    def test_a_well_formed_key_parses(self):
        key = crypto.RsaPublicKey.from_jwk(jwk())
        self.assertEqual(key.kid, KID)
        self.assertEqual(key.modulus, TEST_N)
        self.assertEqual(key.exponent, PUBLIC_EXPONENT)

    def test_a_non_rsa_key_is_refused(self):
        assert_code(self, "unsupported_key_type", crypto.RsaPublicKey.from_jwk,
                    {"kty": "EC", "crv": "P-256"})

    def test_an_encryption_key_is_not_accepted_for_signing(self):
        assert_code(self, "unsupported_key_use", crypto.RsaPublicKey.from_jwk,
                    jwk(use="enc"))

    def test_a_key_pinned_to_another_algorithm_is_refused(self):
        assert_code(self, "unsupported_key_algorithm", crypto.RsaPublicKey.from_jwk,
                    jwk(alg="RS512"))

    def test_a_short_modulus_is_refused(self):
        """A small key is how a forged JWKS slips past."""
        assert_code(self, "key_too_small", crypto.RsaPublicKey.from_jwk,
                    jwk(n=(1 << 1023) | 1))

    def test_an_even_or_tiny_exponent_is_refused(self):
        assert_code(self, "unsupported_exponent", crypto.RsaPublicKey.from_jwk, jwk(e=4))
        assert_code(self, "unsupported_exponent", crypto.RsaPublicKey.from_jwk, jwk(e=3))

    def test_a_missing_modulus_is_refused(self):
        entry = jwk()
        entry.pop("n")
        assert_code(self, "malformed_jwk", crypto.RsaPublicKey.from_jwk, entry)

    def test_a_key_that_is_not_an_object_is_refused(self):
        assert_code(self, "malformed_jwk", crypto.RsaPublicKey.from_jwk, ["not", "a", "key"])


class JwtVerification(unittest.TestCase):
    def test_a_valid_token_returns_its_claims(self):
        claims = verify(make_jwt({"installation_id": "icfg_abc"}))
        self.assertEqual(claims["installation_id"], "icfg_abc")

    def test_a_bad_signature_is_refused(self):
        assert_code(self, "bad_signature", verify, make_jwt(bad_signature=True))

    def test_alg_none_is_refused(self):
        """`alg: none` is the oldest JWT bypass there is."""
        head = _b64u(json.dumps({"alg": "none"}).encode())
        body = _b64u(json.dumps({"iss": ISSUER, "aud": AUDIENCE, "exp": 9e9}).encode())
        assert_code(self, "unsupported_algorithm", verify, f"{head}.{body}.")

    def test_hs256_is_refused_before_any_key_is_used(self):
        """Algorithm confusion: signing with the public key as an HMAC secret.

        The signature below is a *correct* HMAC-SHA256 of the signing input,
        keyed with the DER of the public key. Every implementation that trusts
        the header's `alg` accepts it.
        """
        head = _b64u(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
        body = _b64u(json.dumps({"iss": ISSUER, "aud": AUDIENCE, "exp": 9e9}).encode())
        signing_input = f"{head}.{body}".encode("ascii")
        secret = json.dumps(jwk()).encode("utf-8")
        signature = hmac.new(secret, signing_input, hashlib.sha256).digest()
        token = f"{head}.{body}.{_b64u(signature)}"
        assert_code(self, "unsupported_algorithm", verify, token)

    def test_other_rsa_variants_are_refused(self):
        for algorithm in ("RS384", "RS512", "PS256", "ES256"):
            with self.subTest(algorithm=algorithm):
                assert_code(self, "unsupported_algorithm", verify,
                            make_jwt(header={"alg": algorithm}))

    def test_crit_is_refused(self):
        assert_code(self, "unsupported_header", verify,
                    make_jwt(header={"crit": ["exp"]}))

    def test_an_unknown_kid_is_refused(self):
        assert_code(self, "unknown_key", verify, make_jwt(kid="rotated-away"))

    def test_a_kid_that_matches_selects_that_key(self):
        keys = test_keys(jwk(kid="first", n=OTHER_N), jwk(kid=KID))
        claims = crypto.verify_jwt(make_jwt(), keys, issuer=ISSUER, audience=AUDIENCE)
        self.assertEqual(claims["installation_id"], "icfg_test")

    def test_another_issuer_is_refused(self):
        assert_code(self, "bad_issuer", verify,
                    make_jwt({"iss": "https://marketplace.evil.test"}))

    def test_another_audience_is_refused(self):
        """A token minted for a different integration must not work here."""
        assert_code(self, "bad_audience", verify, make_jwt({"aud": "oac_someoneElse"}))

    def test_an_audience_list_containing_us_is_accepted(self):
        claims = verify(make_jwt({"aud": [AUDIENCE, "oac_other"]}))
        self.assertEqual(claims["user_role"], "ADMIN")

    def test_an_expired_token_is_refused(self):
        assert_code(self, "token_expired", verify,
                    make_jwt({"exp": int(time.time()) - 7200}))

    def test_a_token_without_exp_is_refused(self):
        token = make_jwt()
        head, body, signature = token.split(".")
        rebuilt = json.loads(base64.urlsafe_b64decode(body + "=="))
        rebuilt.pop("exp")
        head_segment, _ = head, body
        new_body = _b64u(json.dumps(rebuilt, separators=(",", ":")).encode())
        signing_input = f"{head_segment}.{new_body}".encode("ascii")
        resigned = _b64u(sign_rs256(signing_input))
        assert_code(self, "claims_missing", verify, f"{head_segment}.{new_body}.{resigned}")

    def test_a_future_nbf_is_refused(self):
        assert_code(self, "token_not_yet_valid", verify,
                    make_jwt({"nbf": int(time.time()) + 3600}))

    def test_small_clock_skew_is_tolerated(self):
        claims = verify(make_jwt({"exp": int(time.time()) - 10}))
        self.assertEqual(claims["installation_id"], "icfg_test")

    def test_a_malformed_token_is_refused(self):
        for token in ("", "not-a-token", "a.b", "a.b.c.d", "a.b.c"):
            with self.subTest(token=token[:12]):
                assert_code(self, "malformed_token", verify, token)

    def test_non_canonical_base64_is_refused(self):
        """Two encodings of one value would let a token change after signing."""
        head = _b64u(json.dumps({"alg": "RS256", "kid": KID}).encode())
        body = _b64u(json.dumps({"iss": ISSUER, "aud": AUDIENCE, "exp": 9e9}).encode())
        signing_input = f"{head}.{body}".encode("ascii")
        standard = base64.b64encode(sign_rs256(signing_input)).decode()
        assert_code(self, "malformed_token", verify, f"{head}.{body}.{standard}")

    def test_an_oversized_token_is_refused(self):
        huge = make_jwt({"pad": "x" * (crypto.MAX_TOKEN_BYTES + 100)})
        assert_code(self, "malformed_token", verify, huge)

    def test_verification_needs_a_configured_issuer_and_audience(self):
        assert_code(self, "not_configured", crypto.verify_jwt, make_jwt(), KEYS,
                    issuer="", audience=AUDIENCE)


class JwksCache(unittest.TestCase):
    def _document(self, *entries):
        return json.dumps({"keys": list(entries) or [jwk()]}).encode("utf-8")

    def _cache(self, fetcher, ttl=crypto.JWKS_TTL_SECONDS):
        """A cache on a clock the test can move.

        The refresh throttle exists so a flood of unknown kids cannot become a
        flood of outbound requests, which means a test that wants a refetch has
        to advance time rather than just ask twice.
        """
        clock = [1_000_000.0]
        cache = crypto.JwksCache("https://marketplace.vercel.com/x", fetcher, ttl=ttl,
                                 clock=lambda: clock[0], min_refresh=10)
        return cache, clock

    def test_a_fetched_document_becomes_usable_keys(self):
        cache = crypto.JwksCache("https://marketplace.vercel.com/.well-known/jwks",
                                 lambda url: self._document())
        self.assertEqual([key.kid for key in cache.keys()], [KID])

    def test_the_document_is_cached_within_the_ttl(self):
        calls = []

        def fetcher(url):
            calls.append(url)
            return self._document()

        cache = crypto.JwksCache("https://marketplace.vercel.com/x", fetcher)
        cache.keys()
        cache.keys()
        self.assertEqual(len(calls), 1)

    def test_an_unknown_kid_forces_one_refetch(self):
        calls = []

        def fetcher(url):
            calls.append(url)
            return self._document(jwk(kid="rotated"))

        cache, clock = self._cache(fetcher)
        cache.keys()
        clock[0] += 30
        self.assertEqual([key.kid for key in cache.keys(force=True)], ["rotated"])
        self.assertEqual(len(calls), 2)

    def test_a_forced_refetch_is_throttled(self):
        """Otherwise a stream of bogus kids turns into a stream of requests."""
        calls = []

        def fetcher(url):
            calls.append(url)
            return self._document()

        cache, _clock = self._cache(fetcher)
        cache.keys()
        cache.keys(force=True)
        cache.keys(force=True)
        self.assertEqual(len(calls), 1)

    def test_a_network_failure_falls_back_to_the_last_good_set(self):
        """Vercel's keys are long-lived; a stale set beats a dashboard outage."""
        cache, clock = self._cache(lambda url: self._document())
        cache.keys()

        def broken(url):
            raise MarketplaceError("dns", code="dns_failed", status=504)

        cache.fetcher = broken
        clock[0] += 30
        self.assertEqual([key.kid for key in cache.keys(force=True)], [KID])

    def test_a_first_boot_network_failure_is_a_failure(self):
        """There is nothing to fall back to, so it must not look like success."""
        def broken(url):
            raise MarketplaceError("dns", code="dns_failed", status=504)

        cache, _clock = self._cache(broken)
        assert_code(self, "dns_failed", cache.keys)

    def test_a_document_that_is_not_json_discards_the_cache(self):
        """A JWKS we cannot read is not a JWKS to keep trusting."""
        cache, clock = self._cache(lambda url: self._document())
        cache.keys()
        cache.fetcher = lambda url: b"<html>503</html>"
        clock[0] += 30
        assert_code(self, "bad_upstream_payload", cache.keys, force=True)
        self.assertEqual(cache._keys, [])

    def test_a_document_with_no_usable_key_is_refused(self):
        cache = crypto.JwksCache("https://marketplace.vercel.com/x",
                                 lambda url: json.dumps({"keys": [
                                     {"kty": "EC"}]}).encode())
        assert_code(self, "bad_upstream_payload", cache.keys)

    def test_an_oversized_document_is_refused(self):
        padding = "x" * (crypto.MAX_JWKS_BYTES + 10)
        cache = crypto.JwksCache("https://marketplace.vercel.com/x",
                                 lambda url: f'{{"keys":[],"p":"{padding}"}}'.encode())
        assert_code(self, "bad_upstream_payload", cache.keys)


class WebhookSignatures(unittest.TestCase):
    SECRET = "client-secret-abcdef123"

    def test_a_correct_signature_is_accepted(self):
        body = b'{"id":"evt_1","type":"integration-configuration.removed"}'
        signature = crypto.webhook_signature(body, self.SECRET)
        self.assertTrue(crypto.verify_webhook(body, signature, self.SECRET))

    def test_a_tampered_body_is_refused(self):
        body = b'{"id":"evt_1"}'
        other = b'{"id":"evt_2"}'
        self.assertFalse(crypto.verify_webhook(
            other, crypto.webhook_signature(body, self.SECRET), self.SECRET))

    def test_a_missing_header_or_secret_is_refused(self):
        body = b"{}"
        self.assertFalse(crypto.verify_webhook(body, None, self.SECRET))
        self.assertFalse(crypto.verify_webhook(body, "abc", ""))
        self.assertFalse(crypto.verify_webhook(body, "", self.SECRET))

    def test_the_signature_is_the_length_sha1_produces(self):
        self.assertEqual(len(crypto.webhook_signature(b"x", self.SECRET)), 40)


class SignedValues(unittest.TestCase):
    SECRET = "signing-secret-abcdef"

    def test_a_value_round_trips(self):
        token = crypto.sign_value(crypto.STATE_PREFIX, "payload", self.SECRET)
        self.assertTrue(crypto.verify_signed_value(crypto.STATE_PREFIX, "payload", token,
                                                   self.SECRET, 600))

    def test_a_value_cannot_be_replayed_under_another_prefix(self):
        """One HMAC key, several purposes: the prefix keeps them apart."""
        token = crypto.sign_value(crypto.STATE_PREFIX, "payload", self.SECRET)
        self.assertFalse(crypto.verify_signed_value(crypto.SESSION_PREFIX, "payload",
                                                    token, self.SECRET, 600))

    def test_a_value_cannot_be_moved_to_another_payload(self):
        token = crypto.sign_value(crypto.STATE_PREFIX, "payload", self.SECRET)
        self.assertFalse(crypto.verify_signed_value(crypto.STATE_PREFIX, "other",
                                                    token, self.SECRET, 600))

    def test_an_expired_value_is_refused(self):
        clock = [1_000_000.0]
        token = crypto.sign_value(crypto.STATE_PREFIX, "payload", self.SECRET, clock=clock.pop)
        future = [1_000_000.0 + 5_000]
        self.assertFalse(crypto.verify_signed_value(
            crypto.STATE_PREFIX, "payload", token, self.SECRET, 60, clock=future.pop))

    def test_a_forged_value_is_refused(self):
        self.assertFalse(crypto.verify_signed_value(crypto.STATE_PREFIX, "payload",
                                                    "1.deadbeef", self.SECRET, 600))

    def test_a_missing_or_malformed_value_is_refused(self):
        for token in (None, "", "abc", "1", "a.b", "1.2.3"):
            with self.subTest(token=token):
                self.assertFalse(crypto.verify_signed_value(
                    crypto.STATE_PREFIX, "payload", token, self.SECRET, 600))


if __name__ == "__main__":
    unittest.main()
