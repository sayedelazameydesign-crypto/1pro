#!/usr/bin/env bash
# Read-only smoke test for a standalone Waha deployment (Render or Vercel + Neon).
#
# Usage:
#   scripts/smoke.sh https://<service>.onrender.com
#   scripts/smoke.sh https://<deployment>.vercel.app
#   scripts/smoke.sh https://<deployment>.<host> https://allowed-origin.example
#
# Notes:
#   * Every call uses --max-time 90 by default. That allows a Render or Neon
#     cold start; override it only with WAHA_SMOKE_TIMEOUT=<seconds>.
#   * The test is deliberately read-only: /api/me is expected to report
#     authenticated:false without a visitor token, so no registration token is
#     emitted and the service's registration rate limit is not consumed.
set -u

BASE="${1:-}"
ORIGIN="${2:-https://sayedelazameydesign-crypto.github.io}"
MAX_TIME="${WAHA_SMOKE_TIMEOUT:-90}"
if [ -z "$BASE" ]; then
  echo "usage: $0 https://<deployment> [allowed-origin]" >&2
  exit 2
fi
BASE="${BASE%/}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
FAILED=0

ok() { printf 'PASS  %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; FAILED=$((FAILED + 1)); }

# curl writes an HTTP code of 000 before exiting non-zero for many transport
# failures. Do not append another 000 with `|| echo 000`, or callers receive
# the ambiguous string 000000. Always normalize every failed request to 000.
request_code() {
  local code status
  code="$(curl -sS --max-time "$MAX_TIME" -w '%{http_code}' "$@")"
  status=$?
  if [ "$status" -ne 0 ]; then
    printf '000'
  else
    printf '%s' "$code"
  fi
}

has_allowed_origin() {
  tr -d '\r' < "$1" | grep -Fqi "Access-Control-Allow-Origin: $ORIGIN"
}

body_preview() {
  head -c 300 "$1" 2>/dev/null || true
}

echo "backend:        $BASE"
echo "allowed origin: $ORIGIN"
echo "request timeout: ${MAX_TIME}s"
echo

echo "== /health (shallow: no database connection or schema initialization) =="
code="$(request_code -o "$TMP/health.json" "$BASE/health")"
if [ "$code" = "200" ] && grep -Eq '"ok": *true' "$TMP/health.json"; then
  ok "/health 200 $(cat "$TMP/health.json")"
else
  bad "/health returned $code: $(body_preview "$TMP/health.json")"
fi

echo "== /readyz (deep: initializes schema and checks the database) =="
code="$(request_code -o /dev/null "$BASE/readyz")"
if [ "$code" = "204" ]; then
  ok "/readyz 204"
else
  bad "/readyz returned $code, expected 204 (check DATABASE_URL, sslmode, and Neon wake-up)"
fi

echo "== /api/me without a token (anonymous identity contract) =="
code="$(request_code -o "$TMP/me.json" -H "Origin: $ORIGIN" "$BASE/api/me")"
if [ "$code" = "200" ] && grep -Eq '"authenticated": *false' "$TMP/me.json" \
   && grep -Eq '"backend": *"standalone"' "$TMP/me.json"; then
  ok "/api/me 200, authenticated=false, backend=standalone"
else
  bad "/api/me returned $code: $(body_preview "$TMP/me.json")"
fi

echo "== CORS preflight from the Pages origin =="
: > "$TMP/preflight.headers"
code="$(request_code -D "$TMP/preflight.headers" -o "$TMP/preflight.body" \
  -X OPTIONS -H "Origin: $ORIGIN" -H 'Access-Control-Request-Method: POST' \
  -H 'Access-Control-Request-Headers: content-type,x-waha-csrf' "$BASE/api/sessions")"
if [ "$code" = "204" ] && has_allowed_origin "$TMP/preflight.headers"; then
  ok "preflight 204 with Access-Control-Allow-Origin: $ORIGIN"
else
  bad "preflight returned $code; headers: $(tr -d '\r' < "$TMP/preflight.headers" | tr '\n' ' ')"
fi

echo "== a foreign origin must be rejected and never echoed =="
: > "$TMP/evil.headers"
code="$(request_code -D "$TMP/evil.headers" -o "$TMP/evil.json" \
  -H 'Origin: https://evil.invalid' "$BASE/api/me")"
if [ "$code" = "403" ] && ! grep -qi '^access-control-allow-origin:' "$TMP/evil.headers"; then
  ok "foreign origin is rejected without a CORS header"
else
  bad "foreign origin returned $code; headers: $(tr -d '\r' < "$TMP/evil.headers" | tr '\n' ' ')"
fi

CONFIG="$(dirname "$0")/../docs/data/config.json"
if [ -f "$CONFIG" ] && ! grep -Fq "\"api_base\": \"$BASE\"" "$CONFIG"; then
  echo "WARN  docs/data/config.json api_base does not point at $BASE yet (Pages stays static)"
fi

echo
if [ "$FAILED" -eq 0 ]; then
  echo "ALL CHECKS PASSED"
else
  echo "$FAILED CHECK(S) FAILED"
fi
exit "$FAILED"
