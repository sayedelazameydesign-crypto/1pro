#!/usr/bin/env bash
# Smoke test for the standalone Waha backend (Render + Neon, explicit frontend origin).
#
# Usage:
#   scripts/smoke.sh https://<api-host>.onrender.com https://<site-host>.onrender.com
#
# Notes:
#   * Every call uses --max-time 90: the first request after Render free-tier
#     idle can take ~50s (Render boot + Neon wake-up).
#   * The visitor token is never printed; only its presence is reported.
set -u

BASE="${1:-}"
ORIGIN="${2:-}"
if [ -z "$BASE" ] || [ -z "$ORIGIN" ]; then
  echo "usage: $0 https://<api-host>.onrender.com https://<frontend-origin>" >&2
  exit 2
fi
BASE="${BASE%/}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
FAILED=0

ok() { printf 'PASS  %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; FAILED=$((FAILED + 1)); }

echo "backend:        $BASE"
echo "allowed origin: $ORIGIN"
echo

echo "== /health (shallow, must not touch the database) =="
code="$(curl -sS --max-time 90 -o "$TMP/health.json" -w '%{http_code}' "$BASE/health" || echo 000)"
if [ "$code" = "200" ] && grep -Eq '"ok": *true' "$TMP/health.json"; then
  ok "/health 200 $(cat "$TMP/health.json")"
else
  bad "/health returned $code: $(cat "$TMP/health.json" 2>/dev/null)"
fi

echo "== /readyz (deep check: wakes Neon, proves initialize() built the schema) =="
code="$(curl -sS --max-time 90 -o /dev/null -w '%{http_code}' "$BASE/readyz" || echo 000)"
if [ "$code" = "204" ]; then
  ok "/readyz 204"
else
  bad "/readyz returned $code, expected 204 (check DATABASE_URL, sslmode, and Neon wake-up)"
fi

echo "== CORS preflight from the configured frontend origin =="
code="$(curl -sS --max-time 90 -D "$TMP/preflight.headers" -o "$TMP/preflight.body" -w '%{http_code}' \
  -X OPTIONS -H "Origin: $ORIGIN" -H 'Access-Control-Request-Method: POST' \
  -H 'Access-Control-Request-Headers: content-type,x-waha-csrf' "$BASE/api/sessions" || echo 000)"
if [ "$code" = "204" ] && grep -qi "^access-control-allow-origin: *$ORIGIN" "$TMP/preflight.headers"; then
  ok "preflight 204 with Access-Control-Allow-Origin: $ORIGIN"
else
  bad "preflight returned $code; headers: $(tr -d '\r' < "$TMP/preflight.headers" | tr '\n' ' ')"
fi

echo "== a foreign origin must not be echoed =="
curl -sS --max-time 90 -D "$TMP/evil.headers" -o /dev/null \
  -H 'Origin: https://evil.invalid' "$BASE/api/skills" || true
if grep -qi '^access-control-allow-origin' "$TMP/evil.headers"; then
  bad "Access-Control-Allow-Origin was echoed for a foreign origin"
else
  ok "foreign origin gets no CORS header"
fi

echo "== register + /api/me (identity round trip) =="
code="$(curl -sS --max-time 90 -o "$TMP/register.json" -w '%{http_code}' \
  -X POST -H 'Content-Type: application/json' -H "Origin: $ORIGIN" -d '{}' \
  "$BASE/api/register" || echo 000)"
if [ "$code" != "201" ]; then
  bad "/api/register returned $code: $(cat "$TMP/register.json" 2>/dev/null)"
else
  token="$(sed -n 's/.*"token":"\([^"]*\)".*/\1/p' "$TMP/register.json")"
  if [ -z "$token" ]; then
    bad "/api/register returned 201 but no token was found"
  else
    ok "/api/register 201 (token received, not printed)"
    code="$(curl -sS --max-time 90 -o "$TMP/me.json" -w '%{http_code}' \
      -H "Authorization: Bearer $token" -H "Origin: $ORIGIN" "$BASE/api/me" || echo 000)"
    if [ "$code" = "200" ] && grep -Eq '"authenticated": *true' "$TMP/me.json" \
       && grep -Eq '"backend": *"standalone"' "$TMP/me.json"; then
      ok "/api/me 200, authenticated, backend=standalone"
    else
      bad "/api/me returned $code: $(head -c 300 "$TMP/me.json" 2>/dev/null)"
    fi
    if grep -Eq '"ai_enabled": *true' "$TMP/me.json"; then
      ok "/api/me reports ai_enabled=true (GEMINI_API_KEY is loaded)"
    else
      bad "/api/me reports ai_enabled=false: set GEMINI_API_KEY on the service"
    fi
  fi
fi

CONFIG="$(dirname "$0")/../docs/data/config.json"
if [ -f "$CONFIG" ] && ! grep -q "\"api_base\": *\"$BASE\"" "$CONFIG"; then
  echo "WARN  docs/data/config.json api_base does not point at $BASE yet (the static frontend is not connected)"
fi

echo
if [ "$FAILED" -eq 0 ]; then
  echo "ALL CHECKS PASSED"
else
  echo "$FAILED CHECK(S) FAILED"
fi
exit "$FAILED"
