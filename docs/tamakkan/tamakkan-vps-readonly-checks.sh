#!/usr/bin/env bash
# Tamakkan - Stage 1 VPS verification (v2). READ-ONLY.
# Scope: Tamakkan and the sibling LLM product on this server. Axis is out of scope.
#
# Server-specific names are NOT stored in the repository. Export them first
# (the administrator has the current values):
#   TB, TF            Tamakkan backend / frontend container names
#   LB, LF            sibling LLM product backend / frontend container names
#   SHARED_BACKEND_TAG, SHARED_FRONTEND_TAG   the legacy shared image tags
#   LEGACY_IMAGE_RE   regex alternation matching the legacy image repositories
#   TAMAKKAN_BACKEND_DIR, TAMAKKAN_FRONTEND_DIR, LLM_BACKEND_DIR, LLM_FRONTEND_DIR
#
# Run:   bash tamakkan-vps-readonly-checks.sh > tamakkan-stage1-report.txt 2>&1
#
# This script does NOT: start/stop/restart/build/pull anything, run docker
# compose up/down, change files or directories, run git fetch/reset, reload
# nginx, write to any database, or connect to MongoDB. The only file created is
# the report you redirect its output into.
# Optional: ALLOW_EXEC=1 additionally runs one HTTP GET /health from INSIDE each
# frontend container (docker exec). It is off by default.
#
# Output safety: every line passes through a final filter that masks IP
# addresses (except 127.0.0.1 / 0.0.0.0), credentials embedded in URLs, and
# common key/credential patterns. Settings are printed only from an explicit
# allowlist; everything else is compared between the two products as
# "same"/"different" without printing values.

set -u
: "${TB:?}" "${TF:?}" "${LB:?}" "${LF:?}" "${SHARED_BACKEND_TAG:?}" "${SHARED_FRONTEND_TAG:?}" "${LEGACY_IMAGE_RE:?}"
: "${TAMAKKAN_BACKEND_DIR:?}" "${TAMAKKAN_FRONTEND_DIR:?}" "${LLM_BACKEND_DIR:?}" "${LLM_FRONTEND_DIR:?}"

# ---------- output filter (applied to ALL output at the end) ----------
sanitize() {
  sed -E \
    -e 's/127\.0\.0\.1/@@LOOP@@/g; s/0\.0\.0\.0/@@ANY@@/g' \
    -e 's/\b([0-9]{1,3}\.){3}[0-9]{1,3}\b/<ip>/g' \
    -e 's/@@LOOP@@/127.0.0.1/g; s/@@ANY@@/0.0.0.0/g' \
    -e 's#(://)[^/@[:space:]]+@#\1<cred>@#g' \
    -e 's#mongodb(\+srv)?://[^[:space:]]*#<mongodb-uri-removed>#g' \
    -e 's/(sk-|pk-|AKIA|gh[pousr]_)[A-Za-z0-9_-]{8,}/<key-removed>/g' \
    -e 's/-----BEGIN[^-]*-----.*/<key-material-removed>/'
}

main() {
hr() { printf '\n==================== %s\n' "$*"; }
exists() { docker inspect "$1" >/dev/null 2>&1; }
envof() { docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$1" 2>/dev/null | grep -E "^$2=" | head -1 | cut -d= -f2-; }
fp() { if [ -n "$1" ]; then printf '%s' "$1" | sha256sum | cut -c1-16; else echo unset; fi; }
cmp2() { # compare one setting across the two backends (or two frontends) without printing it
  local a b; a=$(fp "$(envof "$1" "$3")"); b=$(fp "$(envof "$2" "$3")")
  if [ "$a" = unset ] && [ "$b" = unset ]; then echo "both unset"
  elif [ "$a" = unset ] || [ "$b" = unset ]; then echo "set in only one"
  elif [ "$a" = "$b" ]; then echo "SAME"; else echo "different"; fi; }
# Host part of a MongoDB URI. -n/p: a non-matching value prints NOTHING (never the raw URI).
mongo_hostport() { printf '%s' "$(envof "$1" MONGODB_URI)" | sed -nE 's#^mongodb(\+srv)?://([^@/]*@)?([^/?]+).*#\3#p'; }
host_class() { # classify without revealing the address
  local h="${1%%:*}" p="${1##*:}"; [ "$p" = "$1" ] && p="(default)"
  case "$h" in
    "") echo "unknown";;
    localhost|127.*) echo "loopback (inside the container itself), port $p";;
    host.docker.internal) echo "the Docker host, port $p";;
    10.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[01].*) echo "private network address, port $p";;
    *[a-zA-Z]*) echo "hostname (DNS name), port $p";;
    *) echo "public address, port $p";;
  esac; }
SAFE='^(ENVIRONMENT|PROJECT_NAME|MONGODB_DB_NAME|EMAIL_VERIFICATION_BASE_URL|EMAIL_BRAND_NAME|EMAIL_DELIVERY_MODE|CORS_ORIGINS|LIVEKIT_AGENT_NAME|AUTH_COOKIE_PREFIX|NEXT_PUBLIC_APP_BRAND|NODE_ENV)='
BLOCK=""   # findings that block the Tamakkan H1 deploy
NOTE=""    # findings to report but not blocking H1

hr "0. Run context"
echo "user: $(whoami)   time (UTC): $(date -u +%FT%TZ)   host fingerprint: $(hostname | sha256sum | cut -c1-12)"
docker version --format 'docker server {{.Server.Version}}' 2>/dev/null || echo "docker: NOT ACCESSIBLE for this user (report as unavailable)"

hr "Q1. Are Tamakkan and the sibling LLM product on this same VPS?"
present=0
for c in $TB $TF $LB $LF; do
  if exists "$c"; then present=$((present+1))
    docker inspect -f '{{.Name}}  state={{.State.Status}}  started={{.State.StartedAt}}  restart={{.HostConfig.RestartPolicy.Name}}
      image_id={{.Image}}
      image_tag_in_config={{.Config.Image}}
      compose_project={{index .Config.Labels "com.docker.compose.project"}}  dir={{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$c"
  else echo "/$c  -> NOT on this server"; fi
done
{ exists $TB || exists $TF; } && { exists $LB || exists $LF; } && same_vps=yes || same_vps=no
echo ">>> Both products on this server: $same_vps"

echo "--- where the shared tags point right now:"
for t in "$SHARED_BACKEND_TAG" "$SHARED_FRONTEND_TAG"; do
  id=$(docker image inspect "$t" -f '{{.Id}}' 2>/dev/null || true)
  [ -z "$id" ] && { echo "$t -> (no such image)"; continue; }
  who=""
  for c in $TB $TF $LB $LF; do exists "$c" && [ "$(docker inspect -f '{{.Image}}' "$c")" = "$id" ] && who="$who ${c}"; done
  echo "$t -> $id   used by running container(s):${who:- none}"
done
if [ "$same_vps" = yes ]; then
  for pair in "$TB $LB $SHARED_BACKEND_TAG" "$TF $LF $SHARED_FRONTEND_TAG"; do
    set -- $pair; tid=$(docker image inspect "$3" -f '{{.Id}}' 2>/dev/null || true)
    exists "$1" && [ -n "$tid" ] && [ "$(docker inspect -f '{{.Image}}' "$1")" != "$tid" ] && \
      NOTE="$NOTE\n- $3 currently points to a DIFFERENT image than Tamakkan's running $1 (shared-tag collision is live)."
    exists "$2" && [ -n "$tid" ] && [ "$(docker inspect -f '{{.Image}}' "$2")" != "$tid" ] && \
      NOTE="$NOTE\n- $3 currently points to a DIFFERENT image than the sibling LLM product's running $2 (shared-tag collision is live)."
  done
  BLOCK="$BLOCK\n- Shared image tags on one server: Tamakkan must switch to its own image names before deploying H1 (planned step)."
fi

echo "--- rollback images present (Tamakkan/legacy tags and untagged images):"
docker images --format '{{.Repository}}:{{.Tag}}  {{.ID}}  created {{.CreatedSince}}  {{.Size}}' | grep -iE "^(${LEGACY_IMAGE_RE}|tamakkan|<none>)" || echo "(none)"

hr "Q1b. Deployment directories (known paths only)"
for d in "$TAMAKKAN_BACKEND_DIR" "$TAMAKKAN_FRONTEND_DIR" "$LLM_BACKEND_DIR" "$LLM_FRONTEND_DIR"; do
  [ -d "$d" ] || { echo "--- $d: absent"; continue; }
  echo "--- $d"
  git -C "$d" remote -v 2>/dev/null | head -1
  echo "    HEAD $(git -C "$d" rev-parse --short HEAD 2>/dev/null) on branch $(git -C "$d" rev-parse --abbrev-ref HEAD 2>/dev/null)"
  n=$(git --no-optional-locks -C "$d" status --porcelain 2>/dev/null | wc -l); echo "    local modifications on server: $n file(s)"
  echo "    compose/env files:"; ls -la "$d" 2>/dev/null | awk '/docker-compose|compose\.ya?ml|\.env/ {print "      " $1, $NF}'
done

hr "Q2. Which database does each running backend use? (no credentials, no DB access)"
for c in $TB $LB; do
  exists "$c" || { echo "--- $c: not on this server"; continue; }
  hp=$(mongo_hostport "$c")
  echo "--- $c"
  echo "    MONGODB_DB_NAME : $(envof "$c" MONGODB_DB_NAME)"
  echo "    Mongo server    : $(host_class "$hp")"
  echo "    startup log     : $(docker logs "$c" 2>&1 | grep -m1 -oE "Connected to MongoDB database '[^']*'" || echo '(not in retained logs)')"
done
if exists $TB && exists $LB; then
  ha=$(mongo_hostport $TB); hb=$(mongo_hostport $LB)
  if [ -z "$ha" ] || [ -z "$hb" ]; then hs="unknown"; elif [ "$ha" = "$hb" ]; then hs="SAME"; else hs="different"; fi
  dn=$(cmp2 $TB $LB MONGODB_DB_NAME)
  echo "same Mongo server   : $hs"
  echo "same database name  : $dn"
  echo "same full URI       : $(cmp2 $TB $LB MONGODB_URI)"
  [ "$hs" = SAME ] && [ "$dn" = SAME ] && BLOCK="$BLOCK\n- BOTH BACKENDS USE THE SAME DATABASE on the same server. Stop: escalate before any Tamakkan deploy."
fi

hr "Q3. LiveKit project / agent"
for c in $TB $LB; do
  exists "$c" || continue
  lk=$(envof "$c" LIVEKIT_URL); sub=$(printf '%s' "$lk" | sed -nE 's#^wss?://([^./]+).*#\1#p')
  echo "$c  LiveKit project: ${sub:0:4}…(masked)  agent name setting: $(envof "$c" LIVEKIT_AGENT_NAME)"
done
echo "(Tamakkan's code default agent name is mariam-agent; the sibling LLM product's code hard-codes mariam-agent.)"
if exists $TB && exists $LB; then
  lp=$(cmp2 $TB $LB LIVEKIT_URL)
  echo "same LiveKit project (LIVEKIT_URL)   : $lp"
  echo "same LiveKit API key                 : $(cmp2 $TB $LB LIVEKIT_API_KEY)"
  echo "same agent callback key              : $(cmp2 $TB $LB AGENT_INTERNAL_API_KEY)"
  [ "$lp" = SAME ] && NOTE="$NOTE\n- Same LiveKit project and same agent name: live sessions can reach the wrong product's agent. Not a blocker for H1 (frontend only) but blocks the next Tamakkan backend/agent release."
fi
echo "--- agent workers running on this server (Tamakkan/sibling LLM product only):"
docker ps -a --format '{{.Names}} {{.Image}} {{.Status}}' | grep -iE 'agent|livekit|mariam' | grep -viE 'axis' || echo "(none - agents probably run on LiveKit Cloud; the LiveKit Cloud owner must check)"

hr "Q4. Which backend does each frontend call?"
for c in $TF $LF; do
  exists "$c" || { echo "--- $c: not on this server"; continue; }
  u=$(envof "$c" API_BASE_URL); hp=$(printf '%s' "$u" | sed -nE 's#^https?://([^/]+).*#\1#p')
  echo "--- $c"
  echo "    API_BASE_URL target : ${hp:-unset}  ($(host_class "$hp"))"
  echo "    cookie prefix       : $(envof "$c" AUTH_COOKIE_PREFIX)"
done
echo "--- backend ports published on this server:"
for c in $TB $LB; do exists "$c" && echo "$c: $(docker port "$c" | tr '\n' ' ')"; done
tp=$(printf '%s' "$(envof $TF API_BASE_URL)" | sed -nE 's#^https?://[^/:]+:([0-9]+).*#\1#p')
lp=$(printf '%s' "$(envof $LF API_BASE_URL)" | sed -nE 's#^https?://[^/:]+:([0-9]+).*#\1#p')
echo "Intended: Tamakkan frontend -> Tamakkan API port 4652; sibling LLM frontend -> sibling LLM API port 3524."
exists $TF && [ -n "$tp" ] && [ "$tp" != 4652 ] && BLOCK="$BLOCK\n- Tamakkan frontend points to port $tp, not Tamakkan's backend (4652)."
exists $LF && [ "$lp" = 4652 ] && NOTE="$NOTE\n- SIBLING LLM FRONTEND POINTS TO TAMAKKAN'S BACKEND PORT 4652. Its owner must be told before any Tamakkan backend restart."
echo "--- host-side health checks (read-only GET, no database):"
for p in 4652 3524; do printf 'http://127.0.0.1:%s/api/v1/health -> ' "$p"; curl -s -o /dev/null -m 5 -w '%{http_code}\n' "http://127.0.0.1:$p/api/v1/health" 2>/dev/null || echo "no response"; done
if [ "${ALLOW_EXEC:-0}" = 1 ]; then
  for c in $TF $LF; do exists "$c" || continue
    printf '%s: from inside the container, API_BASE_URL/health -> ' "$c"
    docker exec "$c" node -e 'fetch(process.env.API_BASE_URL.replace(/\/$/,"")+"/health").then(r=>console.log(r.status)).catch(e=>console.log("unreachable:",(e.cause&&e.cause.code)||e.message))' 2>/dev/null || echo "(could not run)"
  done
else echo "(container-internal reachability check skipped; set ALLOW_EXEC=1 to include it)"; fi

hr "Q5. Allowlisted non-secret settings (for context)"
for c in $TB $TF $LB $LF; do exists "$c" || continue
  echo "--- $c"; docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$c" | grep -E "$SAFE" | sort | sed 's/^/    /'
done

hr "Q6. Ports, nginx routing, TLS"
echo "--- listeners on relevant ports (addresses masked):"
ss -ltn 2>/dev/null | awk 'NR==1 || /:(80|443|3524|4652|6356|6868|4662|6366)[[:space:]]/'
echo "--- nginx (read-only config dump, selected directives):"
if nginx -T >/dev/null 2>&1; then
  nginx -T 2>/dev/null | grep -nE '^# configuration file|^\s*(server_name|listen|proxy_pass|client_max_body_size|ssl_certificate|return)\b' | grep -vE '^\s*[0-9]+:\s*#'
else echo "(nginx -T not permitted for this user - report as unavailable; do NOT use sudo workarounds you are not authorised for)"; fi
echo "--- TLS certificates (read directly; no certbot):"
found=0
for f in /etc/letsencrypt/live/*/cert.pem; do [ -r "$f" ] || continue; found=1
  echo "$f"; openssl x509 -noout -subject -enddate -ext subjectAltName -in "$f" 2>/dev/null | sed 's/^/    /'
done
[ $found = 0 ] && echo "(no readable certificates under /etc/letsencrypt/live - report what TLS setup is used, if any)"

hr "Q7. Disk space (room for keeping rollback images)"
df -h / /var/lib/docker 2>/dev/null

hr "SUMMARY"
printf 'Findings that BLOCK deploying Tamakkan H1:%b\n' "${BLOCK:-\n- none detected by this script}"
printf '\nOther findings to report:%b\n' "${NOTE:-\n- none detected by this script}"
echo
echo "DONE - nothing was changed."
}

main 2>&1 | sanitize
