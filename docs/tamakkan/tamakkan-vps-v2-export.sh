#!/usr/bin/env bash
# Tamakkan - production V2 export (v2, hardened). READ-ONLY.
# Run by the VPS administrator. Gives the development team a non-secret
# inventory of what runs in production so it can be compared with
# dev/tamakkan-unified.
#
#   bash tamakkan-vps-v2-export.sh
#   INCLUDE_DIFF=1 bash tamakkan-vps-v2-export.sh   # additionally export a screened diff (see below)
#
# READ-ONLY GUARANTEES (enforced in code, not only by convention)
#  * docker is reachable ONLY through dk(), which allows: version, inspect,
#    image inspect, images, port, ps, compose config. Anything else aborts.
#  * git is reachable ONLY through gt(), which allows: rev-parse, log, status,
#    diff, remote, ls-files; runs with optional locks off and fsmonitor,
#    external diff and textconv disabled (no repo-configured program runs).
#  * It never builds, pulls, restarts, recreates, stops or starts anything, never
#    runs compose up/down, never fetches/resets/checks out, never edits any file
#    outside its own output folder, never connects to MongoDB, never touches nginx,
#    the sibling LLM product or any other product.
#
# SECRET HANDLING (default-deny)
#  * .env files are never copied. Only variable NAMES are listed.
#  * Container settings: only an explicit allowlist of non-secret values is printed;
#    all other values are compared through 12-char SHA-256 fingerprints
#    (unrecoverable for high-entropy secrets).
#  * Compose overrides are NOT copied. They are re-emitted through a structural,
#    default-deny filter: environment/args/labels show variable NAMES only;
#    command/entrypoint/healthcheck/secrets/configs/unknown keys are omitted.
#  * Image labels: only label KEYS, plus values of a 4-label allowlist.
#    Image history, image Config.Env and container Cmd/Entrypoint are never read.
#  * Git diffs are NOT in the report. By default only file names and line counts.
#    With INCLUDE_DIFF=1 a diff file is written only for files that are not
#    secret-like paths, and only if the content scan finds nothing suspicious.
#  * Every output file passes a sanitizer AND a leak scanner. Anything flagged is
#    renamed *.REVIEW-BEFORE-SENDING and must be read by the administrator (or
#    deleted) before anything is sent. The scanner prints line numbers only,
#    never the matched text.
#  * Output directory is created mode 700, files 600.
#
# The administrator remains the last line of defence: read the output before sending.
set -u
umask 077
export GIT_OPTIONAL_LOCKS=0 LC_ALL=C

# Server-specific names are not stored in the repository; export them first:
#   TAMAKKAN_BACKEND_DIR, TAMAKKAN_FRONTEND_DIR   deployment directories
#   TB, TF                                        running container names
#   LEGACY_IMAGE_RE                               regex for the legacy image repositories
: "${TAMAKKAN_BACKEND_DIR:?}" "${TAMAKKAN_FRONTEND_DIR:?}" "${TB:?}" "${TF:?}" "${LEGACY_IMAGE_RE:?}"
BE_DIR="$TAMAKKAN_BACKEND_DIR"
FE_DIR="$TAMAKKAN_FRONTEND_DIR"
OV_DIR=/root/tamakkan-compose
INCLUDE_DIFF="${INCLUDE_DIFF:-0}"

# ----------------------------------------------------------------- guards
die() { echo "ABORT: $*" >&2; exit 1; }

dk() {
  case "${1:-}" in
    version|inspect|images|port|ps) docker "$@" ;;
    image) [ "${2:-}" = inspect ] || die "docker $* is not allowed"; docker "$@" ;;
    compose) [ "${2:-}" = config ] || die "docker $* is not allowed"; docker "$@" ;;
    *) die "docker ${1:-} is not allowed (read-only script)" ;;
  esac
}

gt() { # gt <dir> <subcommand> [args]
  local d=$1 sub=$2; shift 2
  case "$sub" in
    rev-parse|log|status|diff|remote|ls-files) ;;
    *) die "git $sub is not allowed (read-only script)" ;;
  esac
  git --no-optional-locks -c core.fsmonitor=false -C "$d" "$sub" "$@"
}

# ------------------------------------------------------------ sanitizers
sanitize() {
  sed -E \
    -e 's/127\.0\.0\.1/@@LOOP@@/g; s/0\.0\.0\.0/@@ANY@@/g' \
    -e 's/\b([0-9]{1,3}\.){3}[0-9]{1,3}\b/<ip>/g' \
    -e 's/@@LOOP@@/127.0.0.1/g; s/@@ANY@@/0.0.0.0/g' \
    -e 's#(://)[^/@[:space:]]+@#\1<cred>@#g' \
    -e 's#mongodb(\+srv)?://[^[:space:]"]*#<mongodb-uri-removed>#g' \
    -e 's/(sk-|pk-|AKIA|gh[pousr]_|xox[a-z]-)[A-Za-z0-9_-]{8,}/<key-removed>/g' \
    -e 's/eyJ[A-Za-z0-9_-]{10,}(\.[A-Za-z0-9_-]+){0,2}/<jwt-removed>/g' \
    -e 's/(KEY|SECRET|TOKEN|PASSWORD|PASS)([A-Z_]*)([=:][[:space:]]*)[^[:space:]]+/\1\2\3<removed>/g' \
    -e 's/-----BEGIN[^-]*-----.*/<key-material-removed>/'
}

# Structural, default-deny filter for a Compose YAML file (stdin -> stdout).
# Prints structure and non-secret keys; never prints values of unknown keys.
compose_filter() {
  awk '
  function ind_of(s,   m){ match(s,/^[ ]*/); return RLENGTH }
  function keyof(b,   k){ k=b; sub(/^-[ ]*/,"",k); sub(/[ ]*:.*$/,"",k); gsub(/["\047]/,"",k); return k }
  function valof(b,   v){ v=b; if (index(v,":")==0) return ""; sub(/^[^:]*:[ ]*/,"",v); sub(/[ ]*#.*$/,"",v); return v }
  function pad(n,   s){ s=sprintf("%" n "s",""); return s }
  BEGIN { mode=0; modeind=-1; svc=0; svcind=-1; listok=0 }
  {
    line=$0; sub(/\r$/,"",line)
    if (line ~ /^[ ]*(#|$)/) next
    ind=ind_of(line); body=substr(line,ind+1)
    if (mode>0 && ind<=modeind) mode=0
    if (mode==2) next                                   # swallowed block
    if (mode==1) {                                      # environment/args/labels: names only
      n=body; sub(/^-[ ]*/,"",n); sub(/[ ]*[:=].*$/,"",n); gsub(/["\047]/,"",n)
      print pad(ind) n ": (value omitted)"; next
    }
    if (body ~ /^-/) {                                  # list item under a structural key
      if (listok) print line; else print pad(ind) "- (omitted)"
      next
    }
    k=keyof(body); v=valof(body)
    if (ind==0 && k=="services") { print line; svc=1; svcind=-1; listok=0; next }
    if (svc && ind==0) svc=0
    if (svc && svcind<0 && ind>0) svcind=ind
    if (svc && ind==svcind && v=="") { print line; listok=0; next }   # service name
    if (k ~ /^(environment|args|labels)$/) {
      print pad(ind) k ":" (v==""?"":" (inline omitted)")
      if (v=="") { mode=1; modeind=ind }
      listok=0; next
    }
    if (k ~ /^(image|container_name|restart|ports|expose|build|context|dockerfile|target|env_file|volumes|networks|depends_on|name|profiles|platform|pull_policy|extra_hosts|network_mode|user|working_dir)$/) {
      if (k=="extra_hosts" || k=="user" || k=="working_dir") { print pad(ind) k ": (omitted)"; mode=2; modeind=ind; next }
      print line; listok=(v==""); next
    }
    print pad(ind) k ": (omitted)"                       # unknown key: name only, always swallow deeper lines
    mode=2; modeind=ind
    listok=0
  }'
}

# Leak scanner: prints "file:line: reason" (NO matched text). Returns 1 if anything flagged.
LEAK_HITS=0
scan_file() {
  local f=$1 hit=0 tmp; tmp=$(mktemp)
  sed -E 's/\b[0-9a-f]{40,64}\b/<hex>/g; s/sha256:<hex>/<id>/g' "$f" > "$tmp"
  local pat reason
  while IFS='|' read -r reason pat; do
    [ -n "$pat" ] || continue
    local lines; lines=$(grep -nEi -e "$pat" "$tmp" | cut -d: -f1 | tr '\n' ',' | sed 's/,$//')
    if [ -n "$lines" ]; then echo "  REVIEW: $(basename "$f") lines $lines: $reason" >&2; hit=1; fi
  done <<'PATS'
credential-like assignment|(secret|token|passw(or)?d|api[_-]?key|private[_-]?key|credential|authorization|bearer)[A-Za-z0-9_"' ]*[:=][[:space:]]*["']?[^<([:space:]"'][^[:space:]"']{5,}
long token-like string (32+ chars)|[A-Za-z0-9+_=-]{32,}
JWT|eyJ[A-Za-z0-9_-]{10,}
private key block|-----BEGIN
URL with embedded credentials|://[^/[:space:]<]+:[^/[:space:]@<]+@
connection string|mongodb(\+srv)?://
PATS
  rm -f "$tmp"
  if [ $hit -eq 1 ]; then LEAK_HITS=$((LEAK_HITS+1)); mv "$f" "$f.REVIEW-BEFORE-SENDING"; fi
  return $hit
}

fp() { if [ -n "$1" ]; then printf '%s' "$1" | sha256sum | cut -c1-12; else echo unset; fi; }
envof() { dk inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$1" 2>/dev/null | grep -E "^$2=" | head -1 | cut -d= -f2-; }
SAFE='^(ENVIRONMENT|PROJECT_NAME|MONGODB_DB_NAME|LIVEKIT_AGENT_NAME|AUTH_COOKIE_PREFIX|NEXT_PUBLIC_APP_BRAND|NEXT_PUBLIC_SPATIALREAL_APP_ID|SPATIALREAL_APP_ID|SPATIALREAL_REGION|NODE_ENV|CORS_ORIGINS|EMAIL_DELIVERY_MODE|EMAIL_BRAND_NAME|EMAIL_VERIFICATION_BASE_URL|BACKEND_API_URL|COMPOSE_PROJECT_NAME)='
FPVARS='LIVEKIT_URL LIVEKIT_API_KEY LIVEKIT_API_SECRET AGENT_INTERNAL_API_KEY MONGODB_URI OPENAI_API_KEY ULTRAVOX_API_KEY SPATIALREAL_API_KEY'
LABEL_ALLOW='org.opencontainers.image.revision org.opencontainers.image.created org.opencontainers.image.version org.opencontainers.image.source'
# Paths whose diff content is never exported.
SECRETY_PATHSPEC=(':(exclude,glob)**/.env*' ':(exclude,glob)**/*.pem' ':(exclude,glob)**/*.key' ':(exclude,glob)**/*secret*' ':(exclude,glob)**/*credential*' ':(exclude,glob)**/*.p12' ':(exclude,glob)**/id_rsa*')

section() { printf '\n==================== %s\n' "$*"; }

report() {
section "0. Run context"
echo "script: read-only v2-hardened   INCLUDE_DIFF=$INCLUDE_DIFF"
echo "time (UTC): $(date -u +%FT%TZ)   user: $(whoami)   host fingerprint: $(hostname | sha256sum | cut -c1-12)"
dk version --format 'docker server {{.Server.Version}}' 2>/dev/null || echo "docker: not accessible"

for pair in "backend|$BE_DIR" "frontend|$FE_DIR"; do
  IFS='|' read -r NAME DIR <<<"$pair"
  section "1. Source checkout: $NAME  ($DIR)"
  if [ ! -d "$DIR" ]; then echo "ABSENT"; continue; fi
  gt "$DIR" remote -v 2>/dev/null | head -1
  echo "branch : $(gt "$DIR" rev-parse --abbrev-ref HEAD 2>/dev/null)"
  echo "HEAD   : $(gt "$DIR" rev-parse HEAD 2>/dev/null)"
  echo "HEAD date: $(gt "$DIR" log -1 --format=%cI 2>/dev/null)"
  echo "--- last 5 commits (hash, date, subject)"; gt "$DIR" log -5 --format='%h %cI %s' 2>/dev/null
  echo "--- tracked files modified on the server (name status only)"
  gt "$DIR" status --porcelain --untracked-files=no 2>/dev/null
  echo "--- modified tracked files: added/removed line counts (no content)"
  gt "$DIR" diff --no-ext-diff --no-textconv --numstat 2>/dev/null
  echo "--- per-file diff fingerprints (SHA-256, 12 chars; lets us compare with a local diff)"
  gt "$DIR" diff --no-ext-diff --no-textconv --name-only 2>/dev/null | while IFS= read -r f; do
    printf '  %s ~ %s\n' "$(gt "$DIR" diff --no-ext-diff --no-textconv -- "$f" 2>/dev/null | sha256sum | cut -c1-12)" "$f"
  done
  echo "--- untracked, non-ignored files (NAMES ONLY; .env* not listed)"
  gt "$DIR" ls-files --others --exclude-standard 2>/dev/null | grep -vE '(^|/)\.env'
  echo "--- config-like files present (name, size, mtime; content NOT read)"
  ls -la --time-style=long-iso "$DIR" 2>/dev/null | awk '/\.env|override|compose/ {print "  " $1, $5, $6, $7, $NF}'
  if [ -r "$DIR/.env" ]; then
    echo "--- .env variable NAMES (values are never read into this report)"
    grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$DIR/.env" | cut -d= -f1 | sort | sed 's/^/  /'
  fi
  if [ "$INCLUDE_DIFF" = 1 ]; then
    d="$OUT/$NAME-tracked-changes.diff"
    gt "$DIR" diff --no-ext-diff --no-textconv --no-color -- . "${SECRETY_PATHSPEC[@]}" 2>/dev/null | sanitize > "$d"
    echo "--- INCLUDE_DIFF: screened diff written to $(basename "$d") (secret-like paths excluded; scanned below)"
  else
    echo "--- diff content NOT exported (rerun with INCLUDE_DIFF=1 only if asked; it is screened and may be held for review)"
  fi
done

section "2. Compose overrides ($OV_DIR)  -- structural, default-deny re-emit; originals are never copied"
if [ -d "$OV_DIR" ]; then
  ls -la --time-style=long-iso "$OV_DIR" 2>/dev/null | awk '{print "  " $1, $5, $6, $7, $NF}'
  for f in "$OV_DIR"/*.yml "$OV_DIR"/*.yaml; do
    [ -f "$f" ] || continue
    bn=$(basename "$f")
    compose_filter < "$f" | sanitize > "$OUT/override-$bn.filtered"
    echo "--- $bn -> override-$bn.filtered   sha256(original): $(sha256sum "$f" | cut -c1-16)"
  done
else echo "ABSENT"; fi

section "3. Compose resolution (image names only; config bodies are never printed)"
for pair in "backend|$BE_DIR" "frontend|$FE_DIR"; do
  IFS='|' read -r NAME DIR <<<"$pair"
  echo "--- $NAME"
  ( cd "$DIR" 2>/dev/null || exit 0
    echo "COMPOSE_FILE (paths only): $(grep -E '^COMPOSE_FILE=' .env 2>/dev/null | cut -d= -f2-)"
    echo "resolved images:"; dk compose config --images 2>/dev/null | sort -u | sed 's/^/    /'
    echo "resolved project name: $(dk compose config --format json 2>/dev/null | sed -nE 's/^[[:space:]]*"name":[[:space:]]*"([^"]*)".*/\1/p' | head -1)"
  )
done

section "4. Running containers and images (provenance)"
for CTR in "$TB" "$TF"; do
  echo "--- $CTR"
  if ! dk inspect "$CTR" >/dev/null 2>&1; then echo "  not found"; continue; fi
  dk inspect -f '  state={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}n/a{{end}} started={{.State.StartedAt}} restart={{.HostConfig.RestartPolicy.Name}}
  image_id={{.Image}}
  configured_image={{.Config.Image}}
  compose_project={{index .Config.Labels "com.docker.compose.project"}}
  compose_config_files={{index .Config.Labels "com.docker.compose.project.config_files"}}
  working_dir={{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$CTR"
  IMG=$(dk inspect -f '{{.Image}}' "$CTR")
  dk image inspect "$IMG" -f '  image created={{.Created}} tags={{.RepoTags}}' 2>/dev/null
  echo "  image label KEYS (values hidden):"
  dk image inspect "$IMG" -f '{{range $k,$v := .Config.Labels}}{{$k}}{{"\n"}}{{end}}' 2>/dev/null | sort | sed 's/^/    /'
  for lk in $LABEL_ALLOW; do
    v=$(dk image inspect "$IMG" -f "{{index .Config.Labels \"$lk\"}}" 2>/dev/null)
    [ -n "$v" ] && [ "$v" != "<no value>" ] && printf '  label %s = %s\n' "$lk" "$v"
  done
  echo "  published ports: $(dk port "$CTR" | tr '\n' ' ')"
  echo "  --- allowlisted non-secret settings"
  dk inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$CTR" | grep -E "$SAFE" | sort | sed 's/^/    /'
  echo "  --- fingerprints of sensitive settings (12-char SHA-256; values NOT shown)"
  for v in $FPVARS; do printf '    %s ~ %s\n' "$v" "$(fp "$(envof "$CTR" "$v")")"; done
  echo "  --- LiveKit project name only"
  printf '    %s\n' "$(envof "$CTR" LIVEKIT_URL | sed -nE 's#^wss?://([^./]+).*#\1#p')"
done
echo "--- Tamakkan / legacy image tags and ids present (backups included)"
dk images --format '{{.Repository}}:{{.Tag}}  {{.ID}}  created {{.CreatedAt}}' | grep -iE "^(tamakkan|${LEGACY_IMAGE_RE}|<none>)" | sed 's/^/  /'
echo "--- image TAR backups (names/sizes only)"
ls -la /root/image-backups/ 2>/dev/null | awk '{print "  " $1, $5, $6, $7, $8, $NF}' || echo "  none"
echo "--- .env backup files (names/sizes only; contents never read)"
ls -la "$BE_DIR"/.env* "$FE_DIR"/.env* 2>/dev/null | awk '{print "  " $1, $5, $6, $7, $8, $NF}'

section "5. Deploy scripts and recorded state (names only)"
ls -la "$OV_DIR" "$BE_DIR/.tamakkan-deploy" "$FE_DIR/.tamakkan-deploy" 2>/dev/null | awk '{print "  " $1, $5, $6, $7, $8, $NF}'
ls -la /root/*cutover* 2>/dev/null | awk '{print "  " $1, $5, $6, $7, $8, $NF}'

section "6. Health (read-only GET)"
printf '  http://127.0.0.1:4652/api/v1/health -> '
curl -s -o /dev/null -m 5 -w '%{http_code}\n' "http://127.0.0.1:4652/api/v1/health" 2>/dev/null || echo none

section "7. Administrator notes (optional)"
echo "  (write anything the development team should know)"
}

main() {
  OUT="${OUT:-./tamakkan-v2-export-$(date -u +%Y%m%dT%H%M%SZ)}"
  mkdir -m 700 "$OUT" 2>/dev/null || die "cannot create fresh output directory $OUT (exists?)"
  command -v docker >/dev/null || die "docker not found"
  report 2>&1 | sanitize > "$OUT/tamakkan-v2-export.txt"

  echo "Leak scan of every output file (prints line numbers only, never the matched text):"
  for f in "$OUT"/*; do [ -f "$f" ] && scan_file "$f"; done
  chmod 600 "$OUT"/* 2>/dev/null
  echo
  echo "Nothing was changed on this server."
  echo "Output: $(cd "$OUT" && pwd)"
  ls -1 "$OUT" | sed 's/^/  /'
  if [ "$LEAK_HITS" -gt 0 ]; then
    echo
    echo "$LEAK_HITS file(s) were flagged and renamed *.REVIEW-BEFORE-SENDING."
    echo "Open each, delete or redact the flagged lines, then rename it back - or do not send it."
  fi
  echo
  echo "BEFORE SENDING: read every file completely. Send only the files in this folder."
}

if [ "${TAMAKKAN_EXPORT_SOURCE_ONLY:-0}" != 1 ]; then main; fi
