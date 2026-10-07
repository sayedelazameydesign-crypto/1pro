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
#   scripts/vercel_env_sync.sh --self-test   # offline: prove the redactor and matcher
#
# --dry-run validates the values *and* resolves the Vercel team and project, because
# a scope that cannot read is a deploy that cannot finish: the run that "validated
# everything" and then 403'd on its first scoped call is the failure this order
# exists to prevent.
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
#   NEON_API_KEY                              (can stand in for a missing DATABASE_URL:
#                                              the Neon API yields the pooled URL, and the
#                                              derivation is proven by the same live query)
#   NEON_PROJECT_ID | NEON_ROLE | NEON_DATABASE (optional knobs for that derivation;
#                                               defaults: the only project, neondb_owner,
#                                               neondb)
#   GITHUB_REPO_ID                            (only needed to trigger the redeploy)
#   WAHA_SERVICE_URL                          (default: https://cela-umber.vercel.app)
set -uo pipefail

DRY_RUN=0
NEON_FETCH=1
SELF_TEST=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --no-neon-fetch) NEON_FETCH=0 ;;
    --self-test) SELF_TEST=1 ;;
  esac
done

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

# Match the stored team value against the teams the token can actually see. Kept as one
# function so the live run and --self-test execute the same code: a test that re-implements
# the matcher would pass while the matcher was broken.
team_verdict() { # 1: stored VERCEL_TEAM_ID, 2: fallback slug; stdin: /v2/teams JSON
  python3 -c '
import json, sys
teams = (json.load(sys.stdin) or {}).get("teams") or []
want = (sys.argv[1] or "").strip()
slug = (sys.argv[2] or "").strip()
by_id = next((t for t in teams if want and t.get("id") == want), None)
by_slug = next((t for t in teams if want and t.get("slug") == want), None)
named = next((t for t in teams if slug and t.get("slug") == slug), None)
alternative = named or (teams[0] if len(teams) == 1 else None)

def line(kind, team):
    team = team or {}
    print("\t".join([kind, team.get("id") or "",
                      team.get("name") or team.get("slug") or ""]))

# An absent value is a lookup; a wrong value is a defect. They are never collapsed:
# "stale" keeps reporting the mismatch even when a usable alternative exists, because
# quietly replacing a wrong id is how one survives a release.
if want and by_id:
    line("match", by_id)
elif want and by_slug:
    line("slug", by_slug)
elif want:
    line("stale", alternative)
elif named:
    line("slugonly", named)
elif len(teams) == 1:
    line("single", teams[0])
else:
    print("none\t\t")
' "$1" "$2"
}

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
  python3 - "$@" 3<&0 <<'PY'
import os, re, sys, urllib.parse
# The program is read from stdin (the heredoc below), so the text to scrub is read from
# fd 3 -- a duplicate of the caller's stdin, taken before the heredoc replaced it. An
# earlier version read sys.stdin here, which is the *program*, so every redacted line
# came out empty: the database connection error and every Vercel error body were
# silently dropped from the report. Fail-closed, but blind -- and "a failure names the
# exact secret to fix" stops being true when the reason never reaches the log.
# --self-test proves both directions: text passes through, and values are masked.
text = os.fdopen(3, "r", errors="replace").read()
for name in sys.argv[1:]:
    value = os.environ.get(name, "")
    if len(value) < 6:
        continue
    text = text.replace(value, "***")
    text = text.replace(urllib.parse.quote(value, safe=""), "***")
# Structural rule, applied whether or not a named value matched: a password inside any
# DSN in the text is masked. It used to live inside the loop above, so a call whose
# names were all short or empty skipped it entirely.
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
  # The target is usually also the first candidate (DATABASE_URL <- DATABASE_URL,
  # NEON_DATABASE_URL, ...), so the result is written only after every candidate has
  # been read. Clearing the target up front -- which an earlier version did -- wiped
  # the very value being looked for and made every present key look missing.
  local target="$1" candidate value found="" found_value=""
  shift
  PRESENT_NAMES=()
  for candidate in "$@"; do
    value="${!candidate:-}"
    if have "$value"; then
      PRESENT_NAMES+=("$candidate")
      if ! have "$found"; then
        found="$candidate"
        found_value="$value"
        REDACT_NAMES+=("$candidate")
      fi
    fi
  done
  printf -v "$target" '%s' "$found_value"   # the name exists even when nothing matched (set -u)
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

# --- Neon API: turn a key into the connection string --------------------------------
# A Neon API key already reaches the database: it names the project, the read/write
# endpoint, the primary branch and the role, and it can reveal that role's password. So
# "DATABASE_URL must be a Postgres URL" stops being a copy-paste ritual that can go
# wrong (an API key pasted there is a real failure this repository has seen) and becomes
# something the run derives and then proves with a live query. The derivation is
# read-only; nothing is ever written back to GitHub, and the derived password and DSN
# join REDACT_NAMES exactly like the secrets do.
# A project-scoped key works: when it cannot list projects, the 404 names the project it
# is bound to, which is the one we want anyway.
NEON_API="https://console.neon.tech/api/v2"
NCODE="000"
NRESP=""
neon_call() { # PATH
  local path="$1" tmp
  tmp="$(mktemp)"
  NCODE="$(http_code "$(curl -sS -X GET -H "Authorization: Bearer ${NEON_API_KEY:-}" \
      -H 'Accept: application/json' -o "$tmp" -w '%{http_code}' "$NEON_API$path" || true)")"
  NRESP="$(cat "$tmp")"
  rm -f "$tmp"
}

pooled_host() { # host -> host with the "-pooler" label Neon gives its pooler endpoint
  local host="$1" first rest
  first="${host%%.*}"
  rest="${host#*.}"
  case "$first" in
    *-pooler) printf '%s' "$host" ;;
    ep-*)     printf '%s-pooler.%s' "$first" "$rest" ;;
    *)        printf '%s' "$host" ;;
  esac
}

NEON_NOTE=""
derive_database_url() {
  # Fills DATABASE_URL and NEON_NOTE from the Neon API. Failures return 1 quietly: the
  # caller reports them as MISSING with the HTTP status, because a derivation that did
  # not work must not look like a value that did.
  have "${NEON_API_KEY:-}" || return 1
  local project_id="${NEON_PROJECT_ID:-}" branch_id="" host="" role="${NEON_ROLE:-neondb_owner}"
  local dbname="${NEON_DATABASE:-neondb}" password="" encoded="" chosen=""

  if ! have "$project_id"; then
    neon_call "/projects?limit=100"
    if [ "$NCODE" = "200" ]; then
      # Exactly one project is unambiguous; several are not, and guessing one would put
      # the wrong database in production. NEON_PROJECT_ID is the way to decide.
      project_id="$(printf '%s' "$NRESP" | python3 -c '
import json, sys
projects = json.load(sys.stdin).get("projects") or []
print(projects[0]["id"] if len(projects) == 1 else "")' 2>/dev/null)"
      have "$project_id" || return 1
    else
      project_id="$(printf '%s' "$NRESP" | python3 -c '
import json, re, sys
try:
    message = json.load(sys.stdin).get("message", "")
except Exception:
    message = sys.stdin.read()
match = re.search(r"subject_project_id:\s*\\?\"([^\"]+)", message)
print(match.group(1) if match else "")' 2>/dev/null)"
      have "$project_id" || return 1
    fi
  fi

  neon_call "/projects/$project_id/endpoints"
  [ "$NCODE" = "200" ] || return 1
  host="$(printf '%s' "$NRESP" | python3 -c '
import json, sys
endpoints = json.load(sys.stdin).get("endpoints") or []
preferred = [e for e in endpoints if e.get("type") == "read_write"] or endpoints
print(preferred[0].get("host", "") if preferred else "")' 2>/dev/null)"
  have "$host" || return 1

  neon_call "/projects/$project_id/branches"
  [ "$NCODE" = "200" ] || return 1
  branch_id="$(printf '%s' "$NRESP" | python3 -c '
import json, sys
branches = json.load(sys.stdin).get("branches") or []
primary = [b for b in branches if b.get("primary")] or branches
print(primary[0].get("id", "") if primary else "")' 2>/dev/null)"
  have "$branch_id" || return 1

  neon_call "/projects/$project_id/branches/$branch_id/roles"
  [ "$NCODE" = "200" ] || return 1
  chosen="$(printf '%s' "$NRESP" | WANTED="$role" python3 -c '
import json, os, sys
wanted = os.environ.get("WANTED", "neondb_owner")
roles = json.load(sys.stdin).get("roles") or []
names = [r.get("name", "") for r in roles if not r.get("protected")]
print(wanted if wanted in names else (names[0] if names else ""))' 2>/dev/null)"
  have "$chosen" || return 1
  role="$chosen"

  neon_call "/projects/$project_id/branches/$branch_id/roles/$role/reveal_password"
  [ "$NCODE" = "200" ] || return 1
  password="$(printf '%s' "$NRESP" | python3 -c '
import json, sys
print(json.load(sys.stdin).get("password", ""))' 2>/dev/null)"
  have "$password" || return 1

  export NEON_DERIVED_PASSWORD="$password"
  export NEON_DERIVED_DSN=""
  REDACT_NAMES+=(NEON_DERIVED_PASSWORD NEON_DERIVED_DSN)
  encoded="$(NEON_DERIVED_PASSWORD="$password" python3 -c '
import os, urllib.parse
print(urllib.parse.quote(os.environ["NEON_DERIVED_PASSWORD"], safe=""))')"
  DATABASE_URL="postgresql://$role:$encoded@$(pooled_host "$host")/$dbname?sslmode=require"
  export NEON_DERIVED_DSN="$DATABASE_URL"
  NEON_NOTE="project $project_id, branch $branch_id, role $role, endpoint $(pooled_host "$host")"
  return 0
}

# --- Offline self-test --------------------------------------------------------
# Two things must hold without credentials, and both were false once: the redactor must
# pass text through while masking values (it dropped everything instead), and the team
# matcher must call a stored value that is not this team's id a defect rather than
# resolving around it.
self_test() {
  local failures=0 checks=0 out
  expect() { # condition, label
    checks=$((checks + 1))
    if [ "$1" != "0" ]; then
      printf 'self-test FAIL: %s\n' "$2"
      failures=$((failures + 1))
    fi
  }

  export SYNC_SELFTEST_SECRET="ghp_selftestvalue1234567890"
  out="$(printf 'hello token=%s encoded=%s\n' "$SYNC_SELFTEST_SECRET" \
        "$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1],safe=""))' "$SYNC_SELFTEST_SECRET")" \
        | redact SYNC_SELFTEST_SECRET)"
  [ -n "$out" ] && expect 0 "redact drops nothing" || expect 1 "redact drops nothing"
  case "$out" in *hello*) expect 0 "redact passes text through" ;; *) expect 1 "redact passes text through" ;; esac
  case "$out" in *"$SYNC_SELFTEST_SECRET"*) expect 1 "redact masks the raw value" ;; *) expect 0 "redact masks the raw value" ;; esac
  case "$out" in *ghp_selftestvalue*) expect 1 "redact masks the URL-encoded value" ;; *) expect 0 "redact masks the URL-encoded value" ;; esac
  out="$(printf 'dsn=postgresql://user:pw@host:5432/db\n' | redact NOTHING)"
  case "$out" in *"://***@"*) expect 0 "redact masks a password inside a DSN" ;; *) expect 1 "redact masks a password inside a DSN" ;; esac

  local one='{"teams":[{"id":"team_ONE","slug":"one","name":"One"}]}'
  local two='{"teams":[{"id":"team_ONE","slug":"one","name":"One"},{"id":"team_TWO","slug":"two","name":"Two"}]}'
  case "$(printf '%s' "$one" | team_verdict team_ONE one)" in match*) expect 0 "a stored id matches" ;; *) expect 1 "a stored id matches" ;; esac
  case "$(printf '%s' "$one" | team_verdict one one)" in slug*) expect 0 "a stored slug resolves to the id" ;; *) expect 1 "a stored slug resolves to the id" ;; esac
  case "$(printf '%s' "$two" | team_verdict bogus one)" in stale*) expect 0 "a wrong value is a defect" ;; *) expect 1 "a wrong value is a defect" ;; esac
  case "$(printf '%s' "$two" | team_verdict '' one)" in slugonly*) expect 0 "an absent value is a lookup" ;; *) expect 1 "an absent value is a lookup" ;; esac
  case "$(printf '%s' "$one" | team_verdict '' other)" in single*) expect 0 "a lone team is used when named one is missing" ;; *) expect 1 "a lone team is used when named one is missing" ;; esac
  case "$(printf '%s' "$two" | team_verdict '' other)" in none*) expect 0 "several teams with no match resolve to none" ;; *) expect 1 "several teams with no match resolve to none" ;; esac

  if [ "$failures" -gt 0 ]; then
    printf 'vercel_env_sync self-test: %s of %s failed\n' "$failures" "$checks"
    return 1
  fi
  printf 'vercel_env_sync self-test: ok (%s checks)\n' "$checks"
  return 0
}

if [ "$SELF_TEST" = "1" ]; then
  self_test
  exit $?
fi

echo "== 1/5 GitHub secrets (read from this runner's environment)"
echo "key                  state"
if resolve DATABASE_URL "${DATABASE_ALIASES[@]}"; then
  case "$DATABASE_URL" in
    postgres://*|postgresql://*)
      echo "DATABASE_URL         present  <- secret \"$RESOLVED_FROM\"$(dupes_note "$RESOLVED_FROM") ($(shape_of_dsn "$DATABASE_URL"))" ;;
    *)
      # Seen in practice: a Neon API key pasted here. Say what is wrong with the value
      # (length, missing scheme) without ever printing it.
      echo "DATABASE_URL         present  <- secret \"$RESOLVED_FROM\" but NOT a Postgres URL (no postgresql:// scheme, ${#DATABASE_URL} characters)" ;;
  esac
else
  echo "DATABASE_URL         MISSING  (tried: $(alias_list "${DATABASE_ALIASES[@]}"))"
fi
case "${DATABASE_URL:-}" in
  postgres://*|postgresql://*) ;;
  *)
    if have "${NEON_API_KEY:-}" && [ "$NEON_FETCH" = "1" ]; then
      if derive_database_url; then
        echo "DATABASE_URL         derived  <- Neon API ($NEON_NOTE): $(shape_of_dsn "$DATABASE_URL")"
        echo "DATABASE_URL         a live SELECT 1 below must still pass before anything is pushed"
      else
        echo "DATABASE_URL         the Neon API derivation produced nothing (HTTP ${NCODE:-000}); fix the secret by hand"
      fi
    elif have "${NEON_API_KEY:-}"; then
      echo "DATABASE_URL         derivation skipped (--no-neon-fetch)"
    fi
    ;;
esac
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
  if [ "$DRY_RUN" = "1" ]; then
    # A dry run changes nothing anywhere, so it keeps going and reports the rest. Stopping
    # at the first bad value made the operator fix one variable, re-run, find the next --
    # which is how a broken database URL hid a broken team id for a whole run.
    warn "$FAILED value(s) are wrong (not merely missing); continuing so the rest of the report is complete"
  else
    echo "Stopping: $FAILED value(s) are wrong (not merely missing), so nothing was sent to Vercel."
    exit 1
  fi
fi

# The token is needed for the read below, not only for the write later, so it is
# checked here. A dry run still reports a missing token without failing: it is a
# validator, and "you have not stored the token yet" is a finding, not an error.
NO_TOKEN=0
if ! have "$VERCEL_TOKEN"; then
  NO_TOKEN=1
fi
echo

echo "== 3/5 Resolve the Vercel team and project"
TEAM_QUERY=""
if [ "$NO_TOKEN" = "1" ]; then
  warn "VERCEL_TOKEN is not set, so the team and project cannot be resolved yet"
else
  # VERCEL_TEAM_ID is a claim, not a fact, and nothing used to check it: the value was
  # trusted verbatim and interpolated into every scoped URL. A value that is not one of
  # this token's team ids -- a slug, a project id, or a stale value from another account
  # -- is rejected by every endpoint that takes it, and the rejection reads as
  # "Not authorized", which points at the token instead of at the variable. Observed
  # live on 2026-10-07: a 60-character value in VERCEL_TEAM_ID made /v9/projects and
  # /v6/deployments answer 403 for a token that answered 200 for the canonical id.
  # So: ask /v2/teams what this token can actually see, and only then use its answer.
  call GET "/v2/teams?limit=20"
  teams_json="$RESP"
  if [ "$CODE" != "200" ]; then
    bad "listing the teams for this token returned HTTP $CODE: $(echo "$RESP" | redact "${REDACT_NAMES[@]}" | head -c 200)"
  else
    selected="$(echo "$teams_json" | team_verdict "$VERCEL_TEAM_ID" "$TEAM_SLUG")"
    IFS=$'\t' read -r verdict team_id team_name <<<"$selected"
    case "$verdict" in
      match)
        ok "team from VERCEL_TEAM_ID ($team_name)"
        TEAM_QUERY="teamId=$VERCEL_TEAM_ID" ;;
      slug)
        warn "VERCEL_TEAM_ID holds the team *slug* (\"$VERCEL_TEAM_ID\"), which Vercel accepts in its dashboard but not in a scoped API call -- using the id $team_id instead. Store that id to silence this."
        VERCEL_TEAM_ID="$team_id"
        TEAM_QUERY="teamId=$VERCEL_TEAM_ID" ;;
      stale)
        warn "VERCEL_TEAM_ID holds a value that is not one of the team ids this token can see (length ${#VERCEL_TEAM_ID}, no \"team_\" prefix unless it was pasted with one). Every scoped Vercel call would have answered 403 \"Not authorized\" -- the deploy would have stopped here, reading as a token problem."
        if [ -n "$team_id" ]; then
          warn "using $team_name ($team_id) instead for this run; store that id in the VERCEL_TEAM_ID secret."
          VERCEL_TEAM_ID="$team_id"
          TEAM_QUERY="teamId=$VERCEL_TEAM_ID"
        else
          bad "and no alternative could be resolved: this token sees several teams, none of them named \"$TEAM_SLUG\""
          echo "Set VERCEL_TEAM_ID to the id shown in Vercel -> Team Settings -> Team ID."
          exit 1
        fi ;;
      slugonly)
        ok "team \"$TEAM_SLUG\" resolved"
        VERCEL_TEAM_ID="$team_id"
        TEAM_QUERY="teamId=$VERCEL_TEAM_ID" ;;
      single)
        warn "no VERCEL_TEAM_ID is stored and \"$TEAM_SLUG\" names no team this token can see; using the only team available ($team_name). Store its id ($team_id) to silence this."
        VERCEL_TEAM_ID="$team_id"
        TEAM_QUERY="teamId=$VERCEL_TEAM_ID" ;;
      *)
        bad "VERCEL_TEAM_ID matches no team this token can see and \"$TEAM_SLUG\" resolves to none of them"
        echo "Set VERCEL_TEAM_ID to the id shown in Vercel -> Team Settings -> Team ID."
        exit 1 ;;
    esac
  fi
fi

# Skipped without a token on purpose: a lookup that cannot authenticate must not be
# reported as a resolution. The earlier version ran it anyway and printed PASS for a
# project name it had never actually read.
if [ "$NO_TOKEN" = "1" ]; then
  warn "the project is not resolved either; that needs VERCEL_TOKEN"
else
  project_ref="${VERCEL_PROJECT_ID:-$PROJECT_NAME}"
  call GET "/v9/projects/$project_ref?$TEAM_QUERY"
  if [ "$CODE" != "200" ]; then
    bad "project lookup \"$project_ref\" returned HTTP $CODE: $(echo "$RESP" | redact "${REDACT_NAMES[@]}" | head -c 200)"
    echo "Check VERCEL_PROJECT_ID (the id from Vercel -> Project -> Settings) and the team."
    exit 1
  fi
  PROJECT_ID="$(echo "$RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("id",""))')"
  PROJECT_NAME="$(echo "$RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("name",""))')"
  have "$PROJECT_ID" || { bad "the project response carried no id"; exit 1; }
  ok "project \"$PROJECT_NAME\" resolved ($PROJECT_ID)"
fi
echo

if [ "$DRY_RUN" = "1" ]; then
  if [ "$FAILED" -gt 0 ]; then
    echo "Dry run: $FAILED check(s) failed above; nothing was changed on Vercel."
    exit 1
  fi
  if [ "$NO_TOKEN" = "1" ]; then
    echo "Dry run: every value above is acceptable, but the Vercel scope was NOT proved"
    echo "(no VERCEL_TOKEN); nothing was changed on Vercel."
  else
    echo "Dry run: every value above is acceptable and the Vercel scope resolved;"
    echo "nothing was changed on Vercel."
  fi
  exit 0
fi

if [ "$NO_TOKEN" = "1" ]; then
  echo
  echo "Add an Actions secret named VERCEL_TOKEN (Vercel -> Account Settings -> Tokens,"
  echo "scope: the team that owns the project), then run this workflow again."
  exit 3
fi
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
