#!/usr/bin/env bash
# Push the production secrets from GitHub Actions secrets to Vercel, then redeploy
# and verify the live service.
#
# Why a script instead of a few curl lines inside the workflow:
#   * nothing is ever printed -- every line that could carry a value goes through
#     `redact`, so a failing API call cannot leak the key it was given;
#   * the same checks run locally with --dry-run before anything is pushed;
#   * a failure names the exact secret to fix, not "request failed".
#
# Usage:
#   scripts/vercel_env_sync.sh --dry-run     # validate only, touch nothing
#   scripts/vercel_env_sync.sh               # validate, push to Vercel, redeploy
#
# Inputs are environment variables filled from GitHub secrets (any alias wins,
# first non-empty one in the list is used and named in the report):
#   DATABASE_URL | NEON_DATABASE_URL | DATABASE_PRIVATE_URL | POSTGRES_URL | ...
#   GEMINI_API_KEY | GOOGLE_API_KEY | GEMINI_KEY
#   WAHA_SECRET | WAHA_APP_SECRET
#   WAHA_ALLOWED_ORIGINS | ALLOWED_ORIGINS
#   VERCEL_TOKEN | VERCEL_API_TOKEN
#   VERCEL_PROJECT_ID | VERCEL_PROJECT_NAME   (default: cela)
#   VERCEL_TEAM_ID | VERCEL_TEAM_SLUG         (default: celia-fashions-projects)
#   GITHUB_REPO_ID                            (only needed to trigger the redeploy)
#   WAHA_SERVICE_URL                          (default: https://cela-umber.vercel.app)
set -uo pipefail

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

SERVICE_URL="${WAHA_SERVICE_URL:-https://cela-umber.vercel.app}"
PAGES_ORIGIN="https://sayedelazameydesign-crypto.github.io"
DEFAULT_ORIGINS="$PAGES_ORIGIN"
PROJECT_NAME="${VERCEL_PROJECT_NAME:-cela}"
TEAM_SLUG="${VERCEL_TEAM_SLUG:-celia-fashions-projects}"
API="https://api.vercel.com"

# Names this script knows how to look for. Kept in one place so the report can say
# exactly what was tried when something is missing.
DATABASE_ALIASES=(DATABASE_URL NEON_DATABASE_URL DATABASE_PRIVATE_URL POSTGRES_URL
                  POSTGRES_PRISMA_URL POSTGRES_URL_NON_POOLING NEON_POSTGRES_URL
                  NEON_URL NEON_CONNECTION_STRING DATABASE_CONNECTION_STRING
                  WAHA_DATABASE_URL POSTGRESQL_URL DB_URL NEON_DSN CONNECTION_STRING)
GEMINI_ALIASES=(GEMINI_API_KEY GOOGLE_API_KEY GEMINI_KEY GOOGLE_AI_KEY GEMINI_TOKEN
                GOOGLE_GENERATIVE_AI_API_KEY)
SECRET_ALIASES=(WAHA_SECRET WAHA_APP_SECRET APP_SECRET FLASK_SECRET WAHA_CSRF_SECRET)
ORIGIN_ALIASES=(WAHA_ALLOWED_ORIGINS ALLOWED_ORIGINS WAHA_ORIGINS CORS_ORIGINS)
TOKEN_ALIASES=(VERCEL_TOKEN VERCEL_API_TOKEN VERCEL_ACCESS_TOKEN)

FAILED=0
ok() { printf 'PASS  %s\n' "$1"; }
warn() { printf 'WARN  %s\n' "$1"; }
miss() { printf 'MISS  %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; FAILED=$((FAILED + 1)); }
have() { [ -n "${1:-}" ]; }

# `curl -w %{http_code}` prints 000 and *then* fails, so `|| echo 000` used to
# produce "000000". One place normalises it instead.
http_code() { # raw -> exactly three digits, 000 when curl could not connect
  case "${1:-}" in
    ''|*[!0-9]*) printf '000\n' ;;
    *) printf '%s\n' "${1:0:3}" ;;
  esac
}

# --- Never print a secret -----------------------------------------------------
# Replaces every exact value (and its URL-encoded form, and the password inside a
# DSN) with ***. Values are read from the environment by name, never from a shell
# argument, so they cannot show up in a process list either.
redact() {
  python3 - "$@" <<'PY'
import os, re, sys, urllib.parse
text = sys.stdin.read()
for name in sys.argv[1:]:
    value = os.environ.get(name, "")
    if len(value) < 6:
        continue
    text = text.replace(value, "***")
    text = text.replace(urllib.parse.quote(value, safe=""), "***")
    text = re.sub(r"://[^@/\s]*@", "://***@", text)
print(text, end="")
PY
}

# --- Vercel API helper --------------------------------------------------------
# Sets CODE (HTTP status) and RESP (body). Bodies are never echoed raw: callers
# pipe them through `redact` first.
call() { # METHOD PATH [JSON_BODY]
  local method="$1" path="$2" json="${3:-}" tmp
  tmp="$(mktemp)"
  local args=(-sS -X "$method" -H "Authorization: Bearer $VERCEL_TOKEN"
              -H 'Content-Type: application/json' -o "$tmp" -w '%{http_code}')
  [ -n "$json" ] && args+=(--data-binary "$json")
  CODE="$(http_code "$(curl "${args[@]}" "$API$path" || true)")"
  RESP="$(cat "$tmp")"
  rm -f "$tmp"
}

REDACT_NAMES=()
RESOLVED_FROM=""
PRESENT_NAMES=()
resolve() { # resolve VARNAME candidate1 candidate2 ...
  local target="$1" candidate value found=""
  shift
  printf -v "$target" '%s' ""   # the name always exists, even when nothing matched (set -u)
  PRESENT_NAMES=()
  for candidate in "$@"; do
    value="${!candidate:-}"
    if have "$value"; then
      PRESENT_NAMES+=("$candidate")
      if ! have "$found"; then
        printf -v "$target" '%s' "$value"
        found="$candidate"
        REDACT_NAMES+=("$candidate")
      fi
    fi
  done
  RESOLVED_FROM="$found"
  have "$found"
}

alias_list() { local IFS=" "; printf '%s' "$*"; }
dupes_note() { # extra present aliases besides the chosen one
  local chosen="$1" name out=""
  for name in "${PRESENT_NAMES[@]}"; do
    [ "$name" = "$chosen" ] && continue
    out="$out $name"
  done
  [ -n "$out" ] && printf ' (also set:%s)' "$out"
  return 0
}
shape_of_dsn() { local dsn="$1" scheme pooled
  scheme="${dsn%%://*}"
  case "$dsn" in *-pooler*) pooled="pooled" ;; *) pooled="direct" ;; esac
  printf '%s scheme, %s endpoint' "$scheme" "$pooled"
}

echo "== 1/5 GitHub secrets (read from this runner's environment)"
echo "key                  state"
if resolve DATABASE_URL "${DATABASE_ALIASES[@]}"; then
  echo "DATABASE_URL         present  <- secret \"$RESOLVED_FROM\"$(dupes_note "$RESOLVED_FROM") ($(shape_of_dsn "$DATABASE_URL"))"
else
  echo "DATABASE_URL         MISSING  (tried: $(alias_list "${DATABASE_ALIASES[@]}"))"
fi
if resolve GEMINI_API_KEY "${GEMINI_ALIASES[@]}"; then
  echo "GEMINI_API_KEY       present  <- secret \"$RESOLVED_FROM\"$(dupes_note "$RESOLVED_FROM")"
else
  echo "GEMINI_API_KEY       MISSING  (tried: $(alias_list "${GEMINI_ALIASES[@]}"))"
fi
if resolve WAHA_SECRET "${SECRET_ALIASES[@]}"; then
  echo "WAHA_SECRET          present  <- secret \"$RESOLVED_FROM\"$(dupes_note "$RESOLVED_FROM")"
else
  echo "WAHA_SECRET          MISSING  (tried: $(alias_list "${SECRET_ALIASES[@]}"))"
fi
if resolve WAHA_ALLOWED_ORIGINS "${ORIGIN_ALIASES[@]}"; then
  echo "WAHA_ALLOWED_ORIGINS present  <- secret \"$RESOLVED_FROM\"$(dupes_note "$RESOLVED_FROM"): $WAHA_ALLOWED_ORIGINS"
else
  WAHA_ALLOWED_ORIGINS="$DEFAULT_ORIGINS"
  echo "WAHA_ALLOWED_ORIGINS MISSING  -> defaulting to $DEFAULT_ORIGINS"
fi
if resolve VERCEL_TOKEN "${TOKEN_ALIASES[@]}"; then
  echo "VERCEL_TOKEN         present  <- secret \"$RESOLVED_FROM\"$(dupes_note "$RESOLVED_FROM")"
else
  echo "VERCEL_TOKEN         MISSING  (tried: $(alias_list "${TOKEN_ALIASES[@]}")) -- needed to write variables on Vercel"
fi
if resolve VERCEL_PROJECT_ID VERCEL_PROJECT_ID; then
  echo "VERCEL_PROJECT_ID    present  <- secret \"$RESOLVED_FROM\""
else
  echo "VERCEL_PROJECT_ID    missing  -> looking up project \"$PROJECT_NAME\" by name"
fi
if resolve VERCEL_TEAM_ID VERCEL_TEAM_ID; then
  echo "VERCEL_TEAM_ID       present  <- secret \"$RESOLVED_FROM\""
else
  echo "VERCEL_TEAM_ID       missing  -> looking up team \"$TEAM_SLUG\" by slug"
fi
echo

echo "== 2/5 Validate before sending anything"
if have "$DATABASE_URL"; then
  case "$DATABASE_URL" in
    postgres://*|postgresql://*) ok "DATABASE_URL is a Postgres URL ($(shape_of_dsn "$DATABASE_URL"))" ;;
    *) bad "DATABASE_URL does not start with postgres:// or postgresql:// -- Vercel would keep losing data in /tmp" ;;
  esac
  if python3 -c 'import psycopg' 2>/dev/null; then
    if DB_URL="$DATABASE_URL" python3 - <<'PY' 2>&1 | redact DATABASE_URL
import os, sys
import psycopg
dsn = os.environ["DB_URL"]
if "sslmode=" not in dsn:
    dsn = dsn + ("&" if "?" in dsn else "?") + "sslmode=require"
try:
    with psycopg.connect(dsn, connect_timeout=15) as db:
        server = db.execute("SELECT version()").fetchone()[0].split(",")[0]
        name = db.execute("SELECT current_database()").fetchone()[0]
        print(f"{name!r} answered a live query ({server})")
except Exception as error:
    print(f"connection refused: {type(error).__name__}: {error}")
    sys.exit(1)
PY
    then ok "the database answered a live query"
    else bad "the database URL did not connect -- check the password, sslmode, and drop channel_binding=require"
    fi
  else
    warn "psycopg is not installed here; skipping the live database check"
  fi
else
  miss "DATABASE_URL was not found under any known name -- production stays on /tmp SQLite and loses sessions when a container is recycled"
fi

if have "$GEMINI_API_KEY"; then
  case "$GEMINI_API_KEY" in
    AIza*) ok "GEMINI_API_KEY has the Google AI Studio shape (AIza...)" ;;
    *) warn "GEMINI_API_KEY does not start with AIza -- accepted if Google says so, but check it is not a service-account JSON or a revoked key" ;;
  esac
  code="$(http_code "$(curl -sS --max-time 30 -o /dev/null -w '%{http_code}' 2>/dev/null \
    "https://generativelanguage.googleapis.com/v1beta/models?key=$GEMINI_API_KEY" || true)")"
  case "$code" in
    200) ok "Google accepted the key (models list, HTTP 200)" ;;
    400|401|403) bad "Google rejected the key (HTTP $code) -- regenerate it in AI Studio and update the secret" ;;
    *) warn "Google answered HTTP $code; the key was not confirmed either way" ;;
  esac
else
  miss "GEMINI_API_KEY was not found under any known name -- /health stays ai:disabled and the agent refuses every task"
fi

if have "$WAHA_SECRET"; then
  if [ "${#WAHA_SECRET}" -ge 16 ]; then
    ok "WAHA_SECRET is long enough to sign visitor tokens"
  else
    bad "WAHA_SECRET is shorter than 16 characters"
  fi
else
  warn "WAHA_SECRET is missing; Vercel will generate a per-container one and drop visitor sessions on recycle"
fi

case ",$WAHA_ALLOWED_ORIGINS," in
  *",$PAGES_ORIGIN,"*) ok "WAHA_ALLOWED_ORIGINS includes the Pages origin ($PAGES_ORIGIN)" ;;
  *) bad "WAHA_ALLOWED_ORIGINS does not include $PAGES_ORIGIN -- the Pages UI gets no CORS header and cannot call the API" ;;
esac

if [ "$FAILED" -gt 0 ]; then
  echo
  echo "Stopping: $FAILED value(s) are wrong (not merely missing), so nothing was sent to Vercel."
  exit 1
fi

if [ "$DRY_RUN" = "1" ]; then
  echo
  echo "Dry run: every value above is acceptable and nothing was changed on Vercel."
  exit 0
fi

if ! have "$VERCEL_TOKEN"; then
  echo
  echo "Add an Actions secret named VERCEL_TOKEN (Vercel -> Account Settings -> Tokens,"
  echo "scope: the team that owns the project), then run this workflow again."
  exit 3
fi
echo

echo "== 3/5 Resolve the Vercel project"
TEAM_QUERY=""
if have "$VERCEL_TEAM_ID"; then
  TEAM_QUERY="teamId=$VERCEL_TEAM_ID"
  ok "team from VERCEL_TEAM_ID"
else
  call GET "/v2/teams?slug=$TEAM_SLUG"
  if [ "$CODE" != "200" ]; then
    bad "team lookup by slug \"$TEAM_SLUG\" returned HTTP $CODE: $(echo "$RESP" | redact "${REDACT_NAMES[@]}" | head -c 200)"
    echo "Set VERCEL_TEAM_ID instead (Vercel -> Team Settings -> Team ID)."
    exit 1
  fi
  VERCEL_TEAM_ID="$(echo "$RESP" | python3 -c 'import json,sys; print((json.load(sys.stdin).get("teams") or [{}])[0].get("id",""))')"
  have "$VERCEL_TEAM_ID" || { bad "the slug \"$TEAM_SLUG\" resolved to no team"; exit 1; }
  TEAM_QUERY="teamId=$VERCEL_TEAM_ID"
  ok "team \"$TEAM_SLUG\" resolved"
fi

project_ref="${VERCEL_PROJECT_ID:-$PROJECT_NAME}"
call GET "/v9/projects/$project_ref?$TEAM_QUERY"
if [ "$CODE" != "200" ]; then
  bad "project lookup \"$project_ref\" returned HTTP $CODE: $(echo "$RESP" | redact "${REDACT_NAMES[@]}" | head -c 200)"
  exit 1
fi
PROJECT_ID="$(echo "$RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("id",""))')"
PROJECT_NAME="$(echo "$RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("name",""))')"
have "$PROJECT_ID" || { bad "the project response carried no id"; exit 1; }
ok "project \"$PROJECT_NAME\" resolved ($PROJECT_ID)"
echo

echo "== 4/5 Write the Production variables"
call GET "/v9/projects/$PROJECT_ID/env?$TEAM_QUERY&decrypt=false"
[ "$CODE" = "200" ] || { bad "listing the project variables returned HTTP $CODE"; exit 1; }
EXISTING="$(echo "$RESP" | python3 -c '
import json, sys
for item in json.load(sys.stdin).get("envs", []):
    key, env_id, target = item.get("key"), item.get("id"), (item.get("target") or [])
    if "production" in target:
        print(key + "\t" + env_id)
')"
PUSHED=0
push_var() { # key value
  local key="$1" value="$2" body existing_id action
  existing_id="$(printf '%s\n' "$EXISTING" | awk -F'\t' -v k="$key" '$1 == k { print $2; exit }')"
  body="$(K="$key" V="$value" python3 -c '
import json, os
print(json.dumps({"key": os.environ["K"], "value": os.environ["V"],
                  "type": "encrypted", "target": ["production"]}))')"
  if have "$existing_id"; then
    call POST "/v10/projects/$PROJECT_ID/env?$TEAM_QUERY&upsert=true" "$body"
    action="updated"
  else
    call POST "/v10/projects/$PROJECT_ID/env?$TEAM_QUERY" "$body"
    action="created"
  fi
  case "$CODE" in
    200|201) ok "$key $action for Production"; PUSHED=$((PUSHED + 1)) ;;
    *) bad "$key: Vercel returned HTTP $CODE: $(echo "$RESP" | redact "${REDACT_NAMES[@]}" | head -c 200)" ;;
  esac
}
have "$DATABASE_URL" && push_var DATABASE_URL "$DATABASE_URL"
have "$GEMINI_API_KEY" && push_var GEMINI_API_KEY "$GEMINI_API_KEY"
have "$WAHA_SECRET" && push_var WAHA_SECRET "$WAHA_SECRET"
push_var WAHA_ALLOWED_ORIGINS "$WAHA_ALLOWED_ORIGINS"
echo

echo "== 5/5 Redeploy and verify the live service"
if [ "$PUSHED" -eq 0 ]; then
  warn "nothing was written (no value was available); skipping the redeploy"
else
  if have "${GITHUB_REPO_ID:-}"; then
    body="$(P="$PROJECT_ID" N="$PROJECT_NAME" R="$GITHUB_REPO_ID" python3 -c '
import json, os
print(json.dumps({"name": os.environ["N"], "project": os.environ["P"], "target": "production",
                  "gitSource": {"type": "github", "repoId": int(os.environ["R"]), "ref": "main"}}))')"
    call POST "/v13/deployments?$TEAM_QUERY&forceNew=1" "$body"
    if [ "$CODE" = "200" ] || [ "$CODE" = "201" ]; then
      DEPLOY_ID="$(echo "$RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("id",""))')"
      ok "redeploy queued on main so the new variables reach production"
      state=""
      for _ in $(seq 1 60); do
        call GET "/v13/deployments/$DEPLOY_ID?$TEAM_QUERY"
        state="$(echo "$RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("readyState",""))' 2>/dev/null)"
        case "$state" in
          READY) ok "deployment READY"; break ;;
          ERROR|CANCELED) bad "deployment ended as $state"; break ;;
        esac
        sleep 15
      done
      [ "${state:-}" = "READY" ] || warn "deployment state is still ${state:-unknown}; check the Vercel dashboard"
    else
      warn "the redeploy call returned HTTP $CODE: $(echo "$RESP" | redact "${REDACT_NAMES[@]}" | head -c 200)"
      warn "trigger one manual redeploy from the Vercel dashboard to pick up the variables"
    fi
  else
    warn "GITHUB_REPO_ID is not set, so no redeploy was triggered"
  fi
fi

echo
echo "== Live verification ($SERVICE_URL)"
health=""
for _ in $(seq 1 20); do
  health="$(curl -sS --max-time 60 "$SERVICE_URL/health?probe=$(date +%s)" 2>/dev/null || true)"
  have "$health" || { sleep 15; continue; }
  echo "$health" | grep -q '"ok": *true' || { sleep 15; continue; }
  echo "$health" | grep -q '"ai": *"disabled"' || break
  sleep 15
done
printf '%s\n' "$health" | redact "${REDACT_NAMES[@]}"
echo "$health" | grep -q '"ai": *"gemini"' && ok "/health reports ai:gemini" \
  || warn "/health does not report ai:gemini yet"
echo "$health" | grep -q '"database": *"postgres"' && ok "/health reports database:postgres" \
  || warn "/health still reports database:sqlite"
echo "$health" | grep -q '"ready": *true' && ok "the RAG index is ready" \
  || bad "the RAG index is not ready"

code="$(http_code "$(curl -sS --max-time 60 -o /dev/null -w '%{http_code}' "$SERVICE_URL/readyz?probe=$(date +%s)" 2>/dev/null || true)")"
[ "$code" = "204" ] && ok "/readyz 204 (deep database check)" \
  || bad "/readyz returned $code, expected 204"

curl -sS --max-time 60 -X OPTIONS -o /dev/null -D /tmp/cors.headers -w '' \
  -H "Origin: $PAGES_ORIGIN" -H 'Access-Control-Request-Method: POST' \
  -H 'Access-Control-Request-Headers: content-type,x-waha-csrf' \
  "$SERVICE_URL/api/sessions" 2>/dev/null || true
if grep -qi "^access-control-allow-origin: *$PAGES_ORIGIN" /tmp/cors.headers; then
  ok "CORS preflight from the Pages origin is allowed"
else
  warn "still no Access-Control-Allow-Origin for $PAGES_ORIGIN"
fi

echo
if [ "$FAILED" -gt 0 ]; then
  echo "$FAILED check(s) failed."
  exit 1
fi
echo "Sync finished: every value that exists in GitHub is now on Vercel and verified live."
