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
#   * This is a live, state-changing test: the agent task can consume Gemini quota.
#     For a quota-free CORS check, use the OPTIONS command in DEPLOY-VERCEL.md.
#   * Use the stable production domain. An immutable deployment URL may be behind
#     Vercel Deployment Protection; a platform 401 is not proof of an app failure.
#   * The visitor token is never printed; only its presence is reported.
#   * /api/register is capped at 5 per hour per IP on purpose, so a second run in the
#     same hour would otherwise stop at the identity step. Re-use a visitor instead:
#       SMOKE_TOKEN=... SMOKE_CSRF=... scripts/smoke.sh https://<service>.onrender.com
#     (both come from a previous run's /api/register response; neither is echoed here).
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
  -H 'Access-Control-Request-Headers: authorization,content-type,x-waha-csrf' "$BASE/api/sessions" || echo 000)"
# Compare the origin literally (not as a grep regex), and headers/methods as
# case-insensitive comma-separated tokens. A 204 alone is not a browser pass.
header_value() {
  awk -v name="$1" '
    { sub(/\r$/, "") }
    tolower($1) == tolower(name) ":" {
      sub(/^[^:]*:[[:space:]]*/, ""); sub(/[[:space:]]*$/, ""); print
    }' "$TMP/preflight.headers"
}
header_has_token() {
  header_value "$1" | tr ',' '\n' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' | grep -Fxiq -- "$2"
}
if [ "$code" = "204" ] \
   && [ "$(header_value Access-Control-Allow-Origin)" = "$ORIGIN" ] \
   && header_has_token Access-Control-Allow-Methods POST \
   && header_has_token Access-Control-Allow-Headers Authorization \
   && header_has_token Access-Control-Allow-Headers Content-Type \
   && header_has_token Access-Control-Allow-Headers X-Waha-CSRF; then
  ok "preflight 204 allows exact Pages origin, POST, Authorization, Content-Type and X-Waha-CSRF"
else
  bad "preflight returned $code or omitted required CORS permissions; headers: $(tr -d '\r' < "$TMP/preflight.headers" | tr '\n' ' ')"
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
code=""
token=""
csrf=""
if [ -n "${SMOKE_TOKEN:-}" ] && [ -n "${SMOKE_CSRF:-}" ]; then
  token="$SMOKE_TOKEN"
  csrf="$SMOKE_CSRF"
  echo "SKIP  reusing the visitor from SMOKE_TOKEN/SMOKE_CSRF (no new registration)"
else
  code="$(curl -sS --max-time 90 -o "$TMP/register.json" -w '%{http_code}' \
    -X POST -H 'Content-Type: application/json' -H "Origin: $ORIGIN" -d '{}' \
    "$BASE/api/register" || echo 000)"
  if [ "$code" = "201" ]; then
    token="$(sed -n 's/.*"token": *"\([^"]*\)".*/\1/p' "$TMP/register.json")"
    csrf="$(grep -o '"csrf": *"[^"]*"' "$TMP/register.json" | head -1 | sed 's/.*"csrf": *"//; s/"$//')"
  else
    bad "/api/register returned $code: $(cat "$TMP/register.json" 2>/dev/null) -- the limit is 5/hour per IP; re-run with SMOKE_TOKEN/SMOKE_CSRF from an earlier visitor"
  fi
fi
if [ -n "$token" ] && [ -n "$csrf" ]; then
  ok "visitor identity available (token received, never printed)"
  code="$(curl -sS --max-time 90 -o "$TMP/me.json" -w '%{http_code}' \
    -H "Authorization: Bearer $token" -H "Origin: $ORIGIN" "$BASE/api/me" || echo 000)"
  if [ "$code" = "200" ] && grep -Eq '"authenticated": *true' "$TMP/me.json" \
     && grep -Eq '"backend": *"(standalone|promptql)"' "$TMP/me.json"; then
    ok "/api/me 200, authenticated, backend=$(grep -o '"backend": *"[a-z]*"' "$TMP/me.json" | head -1 | sed 's/.*"backend": *"//; s/"//')"
  else
    bad "/api/me returned $code: $(head -c 300 "$TMP/me.json" 2>/dev/null)"
  fi
  if grep -Eq '"ai_enabled": *true' "$TMP/me.json"; then
    ok "/api/me reports ai_enabled=true (GEMINI_API_KEY is loaded)"
  else
    bad "/api/me reports ai_enabled=false: set GEMINI_API_KEY on the service"
  fi
fi


echo "== agent runtime: config =="
code="$(curl -sS --max-time 90 -o "$TMP/agent-config.json" -w '%{http_code}' "$BASE/api/agent/config" || echo 000)"
if [ "$code" = "200" ] && grep -Eq '"enabled": *true' "$TMP/agent-config.json"; then
  tools="$(grep -o '"name": *"[a-z_]*"' "$TMP/agent-config.json" | wc -l | tr -d ' ')"
  ok "/api/agent/config 200, agent enabled, $tools tools advertised"
  if grep -Eq '"execution": *\{' "$TMP/agent-config.json"; then
    ok "R6 execution contract advertised: $(grep -o '"mode": *"[a-z]*"' "$TMP/agent-config.json" | head -1), $(grep -o '"max_steps": *[0-9]*' "$TMP/agent-config.json" | head -1) on this host (new_knobs $(grep -o '"new_knobs": *[0-9]*' "$TMP/agent-config.json" | head -1 | sed 's/.*: *//'))"
  else
    bad "/api/agent/config has no execution block: the deployed backend predates R6 (mode/budget are unpublished)"
  fi
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
  # `"id"` alone is ambiguous: an inline response embeds the finished task, whose tool-call
  # rows carry ids shaped like `tc_...`. Anchor on the task object or the first poll fetches
  # a row that does not exist and the run looks broken when only the script is.
  # Flask sorts response keys, so `"task"` is not followed by `"id"`, and `calls[].id`
  # (`tc_...`) would be picked up by a naive first-match. A task id is a UUID: match that.
  task_id="$(grep -o '"id":"[0-9a-f]\{8\}-[0-9a-f]\{4\}-[0-9a-f]\{4\}-[0-9a-f]\{4\}-[0-9a-f]\{12\}"' \
    "$TMP/task.json" | head -1 | tr -d '"' | sed 's/^id://; s/^://')"
  run_mode="$(grep -o '"mode": *"[a-z]*"' "$TMP/task.json" | head -1 | sed 's/.*"mode": *"//; s/"$//')"
  status=""
  if [ -z "$task_id" ]; then
    bad "task creation returned no task id: $(head -c 200 "$TMP/task.json")"
  else
    ok "task $task_id accepted (mode=${run_mode:-unknown})"
    # The event feed is the only response where "status" is unambiguous (the task
    # detail embeds step and tool-call statuses too), so poll that and fetch the
    # full task once, at the end. An inline (R6) run is already terminal in the 201
    # body, so polling it would only burn two minutes and prove nothing new.
    if [ "$run_mode" != "inline" ]; then
      status="queued"
      for _ in $(seq 1 40); do
        sleep 3
        curl -sS --max-time 90 -o "$TMP/agent-feed.json" -H "Authorization: Bearer $token" \
          -H "Origin: $ORIGIN" "$BASE/api/agent/tasks/$task_id/events?cursor=0" || true
        status="$(grep -o '"status": *"[a-z_]*"' "$TMP/agent-feed.json" | head -1 | sed 's/.*: *"//; s/"$//')"
        case "$status" in completed|failed|cancelled|interrupted|expired) break;; esac
      done
    fi
    curl -sS --max-time 90 -o "$TMP/agent-feed.json" -H "Authorization: Bearer $token" \
      -H "Origin: $ORIGIN" "$BASE/api/agent/tasks/$task_id/events?cursor=0" || true
    if [ -z "$status" ]; then
      # Read the task status from the feed even when there was nothing to wait for:
      # `"status"` in the create body belongs to a step or a tool call, not to the task.
      status="$(grep -o '"status": *"[a-z_]*"' "$TMP/agent-feed.json" | tail -1 | sed 's/.*"status": *"//; s/"$//')"
    fi
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
    echo "== agent runtime: memory and artifacts (what the task leaves behind) =="
    curl -sS --max-time 90 -o "$TMP/memory.json" -X POST -H "Authorization: Bearer $token" \
      -H "Origin: $ORIGIN" -H 'Content-Type: application/json' -H "X-Waha-CSRF: $csrf" \
      -d '{"kind":"note","content":"ملاحظة smoke: أفضّل الأمثلة القصيرة."}' \
      "$BASE/api/agent/memory" >/dev/null 2>&1 || true
    mem_id="$(grep -o '"id":[0-9 ]*[0-9]' "$TMP/memory.json" | head -1 | tr -d ' id:"' | sed 's/[^0-9]//g')"
    curl -sS --max-time 90 -o "$TMP/memory-list.json" -H "Authorization: Bearer $token" \
      -H "Origin: $ORIGIN" "$BASE/api/agent/memory" || true
    if [ -n "$mem_id" ] && grep -q 'أفضّل الأمثلة القصيرة' "$TMP/memory-list.json" \
       && grep -Eq '"items":[0-9 ]*[1-9]' "$TMP/memory.json"; then
      ok "memory write + read-back works for this visitor"
    else
      bad "memory round trip failed (id='${mem_id:-none}'): $(head -c 200 "$TMP/memory-list.json")"
    fi
    art_id="$(grep -o '"artifacts": *\[[^]]*"id":[0-9 ]*[0-9]' "$TMP/task.json" | grep -o '[0-9]*$' | head -1)"
    if [ -n "$art_id" ]; then
      curl -sS --max-time 90 -o "$TMP/artifact.json" -H "Authorization: Bearer $token" \
        -H "Origin: $ORIGIN" "$BASE/api/agent/artifacts/$art_id" || true
      if grep -Eq '"content": *"' "$TMP/artifact.json"; then
        ok "task artifact $art_id is readable back by its owner"
      else
        bad "artifact $art_id not readable: $(head -c 200 "$TMP/artifact.json")"
      fi
    else
      echo "SKIP  this task produced no artifact (fine: the run above still proved tools ran)"
    fi
    curl -sS --max-time 90 -o /dev/null -X POST -H "Authorization: Bearer $token" \
      -H "Origin: $ORIGIN" -H 'Content-Type: application/json' -H "X-Waha-CSRF: $csrf" \
      -d '{}' "$BASE/api/agent/memory/$mem_id/delete" >/dev/null 2>&1 || true

    curl -sS --max-time 90 -o /dev/null -X POST -H "Authorization: Bearer $token" \
      -H "Origin: $ORIGIN" -H 'Content-Type: application/json' -H "X-Waha-CSRF: $csrf" \
      -d '{}' "$BASE/api/agent/tasks/$task_id/delete" >/dev/null 2>&1 || true


  fi
fi

echo "== R1/R2 data plane: /health.rag and /api/search (must work with no model key) =="
if grep -Eq '"ready": *true' "$TMP/health.json"; then
  ok "/health advertises a ready rag index ($(grep -o '"chunks": *[0-9]*' "$TMP/health.json" | head -1), $(grep -o '"vector_gate": *"[a-z-]*"' "$TMP/health.json" | head -1))"
else
  bad "/health.rag not ready: $(grep -o '"rag": *{[^}]*}' "$TMP/health.json" 2>/dev/null | head -c 200) — data/rag may be missing from the deployed bundle (check deploy_doctor)"
fi
curl -sS --max-time 90 -o "$TMP/search-hit.json" -G --data-urlencode 'q=كيف أحدد جمهور الرسالة' \
  --data-urlencode 'k=3' "$BASE/api/search" || true
if grep -Eq '"evidence": *"RAG_LOCAL"' "$TMP/search-hit.json" && grep -Eq '"citation": *"SKL[0-9]{3}#[a-z]+"' "$TMP/search-hit.json"; then
  ok "/api/search returns RAG_LOCAL evidence with a verbatim citation"
else
  bad "/api/search did not return a cited RAG_LOCAL result: $(head -c 200 "$TMP/search-hit.json")"
fi
curl -sS --max-time 90 -o "$TMP/search-miss.json" -G --data-urlencode 'q=ما سعر صرف الليرة اليوم' "$BASE/api/search" || true
if grep -Eq '"decision": *"deferred"' "$TMP/search-miss.json" && grep -Eq '"result_count": *0' "$TMP/search-miss.json"; then
  ok "/api/search answers an out-of-catalog question with 0 results + deferred (no invented answer)"
else
  bad "/api/search on an out-of-catalog query: $(head -c 200 "$TMP/search-miss.json")"
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
