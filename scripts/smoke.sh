#!/usr/bin/env bash
# Smoke test for the standalone Waha backend (Render + Neon, GitHub Pages origin).
#
# Usage:
#   scripts/smoke.sh https://<service>.onrender.com
#   scripts/smoke.sh https://<service>.onrender.com https://sayedelazameydesign-crypto.github.io
#
# Notes:
#   * Every call uses --max-time 90: the first request after Render free-tier
#     idle can take ~50s (Render boot + Neon wake-up).
#   * The visitor token is never printed; only its presence is reported.
set -u

BASE="${1:-}"
ORIGIN="${2:-https://sayedelazameydesign-crypto.github.io}"
if [ -z "$BASE" ]; then
  echo "usage: $0 https://<service>.onrender.com [allowed-origin]" >&2
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

echo "== CORS preflight from the Pages origin =="
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
  csrf="$(grep -o '"csrf": *"[^"]*"' "$TMP/register.json" | head -1 | sed 's/.*"csrf": *"//; s/"$//')"
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


echo "== agent runtime: config =="
code="$(curl -sS --max-time 90 -o "$TMP/agent-config.json" -w '%{http_code}' "$BASE/api/agent/config" || echo 000)"
if [ "$code" = "200" ] && grep -Eq '"enabled": *true' "$TMP/agent-config.json"; then
  tools="$(grep -o '"name": *"[a-z_]*"' "$TMP/agent-config.json" | wc -l | tr -d ' ')"
  ok "/api/agent/config 200, agent enabled, $tools tools advertised"
  AGENT_ENABLED=1
elif [ "$code" = "200" ]; then
  echo "SKIP  agent layer is present but disabled (no model key on the server): $(head -c 120 "$TMP/agent-config.json")"
  AGENT_ENABLED=0
else
  bad "/api/agent/config returned $code (expected 200; the agent routes are part of the standalone app)"
  AGENT_ENABLED=0
fi

if [ "${AGENT_ENABLED:-0}" = "1" ] && [ -n "${token:-}" ] && [ -n "${csrf:-}" ]; then
  echo "== agent runtime: one real task round trip =="
  curl -sS --max-time 90 -o "$TMP/task.json" -X POST \
    -H "Authorization: Bearer $token" -H "Origin: $ORIGIN" -H 'Content-Type: application/json' \
    -H "X-Waha-CSRF: $csrf" \
    -d '{"goal":"احسب متوسط الأرقام 2 و4 و6 واكتب الخلاصة في سطر واحد."}' \
    "$BASE/api/agent/tasks" || true
  task_id="$(grep -o '"id": *"[^"]*"' "$TMP/task.json" | head -1 | sed 's/.*: *"//; s/"$//')"
  if [ -z "$task_id" ]; then
    bad "task creation returned no id: $(head -c 200 "$TMP/task.json")"
  else
    ok "task $task_id accepted"
    # The event feed is the only response where "status" is unambiguous (the task
    # detail embeds step and tool-call statuses too), so poll that and fetch the
    # full task once, at the end.
    status="queued"
    for _ in $(seq 1 40); do
      sleep 3
      curl -sS --max-time 90 -o "$TMP/agent-feed.json" -H "Authorization: Bearer $token" \
        -H "Origin: $ORIGIN" "$BASE/api/agent/tasks/$task_id/events?cursor=0" || true
      status="$(grep -o '"status": *"[a-z_]*"' "$TMP/agent-feed.json" | head -1 | sed 's/.*: *"//; s/"$//')"
      case "$status" in completed|failed|cancelled|interrupted|expired) break;; esac
    done
    curl -sS --max-time 90 -o "$TMP/task.json" -H "Authorization: Bearer $token" \
      -H "Origin: $ORIGIN" "$BASE/api/agent/tasks/$task_id" || true
    if [ "$status" = "completed" ]; then
      ok "task finished: $status ($(grep -o '"ai_calls": *[0-9]*' "$TMP/task.json" | head -1), $(grep -o '"tool_calls": *[0-9]*' "$TMP/task.json" | head -1))"
    else
      bad "task ended as '$status': $(head -c 300 "$TMP/task.json")"
    fi
    if grep -q "task.created" "$TMP/agent-feed.json"; then
      ok "task event log readable ($(grep -o '"type": *"[a-z._]*"' "$TMP/agent-feed.json" | wc -l | tr -d ' ') events)"
    else
      bad "event feed has no task.created entry (task may not be persisted)"
    fi
    curl -sS --max-time 90 -o /dev/null -X POST -H "Authorization: Bearer $token" \
      -H "Origin: $ORIGIN" -H 'Content-Type: application/json' -H "X-Waha-CSRF: $csrf" \
      -d '{}' "$BASE/api/agent/tasks/$task_id/delete" >/dev/null 2>&1 || true
  fi
fi

CONFIG="$(dirname "$0")/../docs/data/config.json"
if [ -f "$CONFIG" ] && ! grep -q "\"api_base\": *\"$BASE\"" "$CONFIG"; then
  echo "WARN  docs/data/config.json api_base does not point at $BASE yet (Pages stays static)"
fi

echo
if [ "$FAILED" -eq 0 ]; then
  echo "ALL CHECKS PASSED"
else
  echo "$FAILED CHECK(S) FAILED"
fi
exit "$FAILED"
