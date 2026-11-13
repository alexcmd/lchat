#!/usr/bin/env bash
# lchat installer: scripts -> ~/.local/bin, page + webcrawler + help cards -> ~/.local/share/lchat,
# Python deps (pip --user), optional llama.cpp Vulkan build, then the models, then the help-card index.
# Re-run it after `git pull` to update: changed cards are re-indexed, removed cards are dropped.
#
#   ./install.sh                 install + download models
#   ./install.sh --link          symlink scripts to this checkout instead of copying (for development)
#   ./install.sh --with-llama    also install a prebuilt llama.cpp (Vulkan, x64) if `llama` is missing
#   ./install.sh --no-models     skip the model download (run `lchat-models` later)
#   ./install.sh --verbose       show every command's output instead of spinners
#   ./install.sh --plain         no colours or animation (also when NO_COLOR is set or output is not a terminal)
#
# Everything the steps print goes to ~/.local/state/lchat/install.log.
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
BIN="$HOME/.local/bin"
SHARE="$HOME/.local/share/lchat"
STATE="$HOME/.local/state/lchat"
LOG="$STATE/install.log"
LLAMA_TAG="${LLAMA_TAG:-b12290}"
LINK=0 WITH_LLAMA=0 MODELS=1 VERBOSE=0 PLAIN=0
for arg in "$@"; do
  case "$arg" in
    --link) LINK=1 ;;
    --with-llama) WITH_LLAMA=1 ;;
    --no-models) MODELS=0 ;;
    --verbose|-v) VERBOSE=1 ;;
    --plain) PLAIN=1 ;;
    -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg (see --help)" >&2; exit 2 ;;
  esac
done

# ===================================================================== UI =====
FANCY=1
{ [ "$PLAIN" = 1 ] || [ -n "${NO_COLOR:-}" ] || [ ! -t 1 ] || [ "${TERM:-dumb}" = dumb ]; } && FANCY=0
if [ "$FANCY" = 1 ]; then
  B=$'\e[1m' D=$'\e[2m' R=$'\e[0m' RED=$'\e[31m' GRN=$'\e[32m' YEL=$'\e[33m' BLU=$'\e[34m' MAG=$'\e[35m' CYN=$'\e[36m'
  OK="${GRN}✓${R}" BAD="${RED}✗${R}" WRN="${YEL}⚠${R}" SKP="${D}–${R}" DOT="${D}·${R}"
  FRAMES=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)
else
  B= D= R= RED= GRN= YEL= BLU= MAG= CYN=
  OK="[ok]" BAD="[FAIL]" WRN="[warn]" SKP="[skip]" DOT="-"
  FRAMES=()
fi

STAGES=("Check system" "Scripts and page" "Help cards" "Python packages" "llama.cpp"
        "Window capture helper" "Models" "Help-card index")
NSTAGES=${#STAGES[@]}
STAGE=0
WARNINGS=()
T0=$SECONDS
CUR_PID=

cols() { local c; c=$(tput cols 2>/dev/null || echo 80); [ "$c" -gt 20 ] 2>/dev/null || c=80; echo "$c"; }
dur() { local s=$1; if [ "$s" -ge 60 ]; then printf '%dm%02ds' $((s/60)) $((s%60)); else printf '%ds' "$s"; fi; }
tidy() { local p=$1; printf '%s' "${p/#$HOME/\~}"; }   # show paths under $HOME as ~/...
log() { printf '%s\n' "$*" >> "$LOG"; }

# Each step runs in a fresh `bash -euo pipefail` (the step functions are exported). Calling a function from
# `run ... || warn` or `if run ...` would switch set -e off for everything inside it - subshells included - so a
# failing command mid-step would go unnoticed. setsid gives the step its own process group, so stopping it also
# stops what it started (pip builds, curl).
step_cmd() {
  STEP=(bash -c 'set -euo pipefail; "$@"' step "$@")
  command -v setsid >/dev/null && STEP=(setsid -w "${STEP[@]}")
  return 0
}
stop_step() {
  kill -TERM -- "-$1" 2>/dev/null || { pkill -TERM -P "$1" 2>/dev/null; kill -TERM "$1" 2>/dev/null; } || true
}
FAILED=()   # steps that failed without stopping the install; any of them makes the exit code 1
fail() { echo "    ${BAD} ${RED}$*${R}"; log "FAILED: $*"; FAILED+=("$*"); }

cleanup() {
  local rc=$?
  [ -n "$CUR_PID" ] && stop_step "$CUR_PID"
  [ -n "$STEP_OUT" ] && rm -f "$STEP_OUT"
  [ "$FANCY" = 1 ] && printf '\e[?25h' || true          # cursor back on
  if [ "$rc" -ne 0 ] && [ "${FINISHED:-0}" = 0 ]; then
    echo
    echo "  ${BAD} ${B}install stopped${R} at stage $STAGE/$NSTAGES (${STAGES[$((STAGE>0?STAGE-1:0))]}) after $(dur $((SECONDS-T0)))"
    echo "  ${D}full log: $(tidy "$LOG")${R}"
  fi
}
trap cleanup EXIT
trap 'echo; echo "  ${YEL}interrupted${R}"; exit 130' INT TERM

rep() { local i; for ((i = 0; i < $2; i++)); do printf '%s' "$1"; done; }
bar() {   # overall progress: done stages out of all
  local done=$1 w=28 f
  f=$(( done * w / NSTAGES ))
  if [ "$FANCY" = 1 ]; then
    printf '%s%s%s%s' "$CYN" "$(rep ━ "$f")" "$D" "$(rep ─ $((w - f)))"
    printf '%s %d%%' "$R" $(( done * 100 / NSTAGES ))
  fi   # plain mode: the [n/N] in each stage header is enough
}

banner() {
  local ver os
  ver=$(git -C "$REPO" describe --always --dirty 2>/dev/null || echo "?")
  os=$( . /etc/os-release 2>/dev/null && echo "${PRETTY_NAME:-$NAME}" || uname -s)
  if [ "$FANCY" = 1 ]; then
    echo
    echo "  ${MAG}╭──────────────────────────────────────────╮${R}"
    echo "  ${MAG}│${R}  ${B}lchat${R} installer  ${D}·  local AI for Linux${R}  ${MAG}│${R}"
    echo "  ${MAG}╰──────────────────────────────────────────╯${R}"
  else
    echo "lchat installer"
  fi
  echo "  ${D}version${R} $ver   ${D}system${R} $os $(uname -m)"
  local mode="copy"; [ "$LINK" = 1 ] && mode="symlink to checkout"
  echo "  ${D}mode${R} $mode   ${D}models${R} $([ "$MODELS" = 1 ] && echo download || echo skip)   ${D}llama.cpp${R} $([ "$WITH_LLAMA" = 1 ] && echo "install if missing" || echo "use existing")"
  echo "  ${D}log${R} $(tidy "$LOG")"
}

stage() {   # stage "title"
  STAGE=$((STAGE+1)); STAGE_T=$SECONDS
  log ""; log "=== [$STAGE/$NSTAGES] $1 ==="
  echo
  echo "  ${B}${BLU}[$STAGE/$NSTAGES]${R} ${B}$1${R}  $(bar $((STAGE - 1)))"
}

ok()   { echo "    ${OK} $*"; log "ok: $*"; }
skip() { echo "    ${SKP} ${D}$*${R}"; log "skip: $*"; }
info() { echo "    ${DOT} ${D}$*${R}"; log "info: $*"; }
warn() { echo "    ${WRN} ${YEL}$*${R}"; log "WARNING: $*"; WARNINGS+=("$*"); }

# run "label" cmd...: run a command (or shell function) with its output in the log; a spinner with the elapsed time
# and the newest output line shows meanwhile. Prints ✓/✗ with the duration; returns the command's exit code.
STEP_OUT=
export PYTHONUNBUFFERED=1   # so the spinner can show what python steps print while they run
run() {
  local label=$1; shift
  local t=$SECONDS rc=0 i=0 last w
  [ -n "$STEP_OUT" ] && rm -f "$STEP_OUT"
  STEP_OUT=$(mktemp)
  log "--- $label: $*"
  step_cmd "$@"
  if [ "$VERBOSE" = 1 ]; then
    echo "    ${CYN}›${R} $label"
    "${STEP[@]}" > >(tee "$STEP_OUT" | sed 's/^/      /') 2>&1 &
  else
    "${STEP[@]}" > "$STEP_OUT" 2>&1 &
  fi
  CUR_PID=$!
  if [ "$FANCY" = 1 ] && [ "$VERBOSE" = 0 ]; then
    printf '\e[?25l'
    local c show=$label; c=$(cols)
    # keep the spinner on one terminal row: a wrapped line cannot be redrawn with \r
    [ "${#show}" -gt $((c - 16)) ] && show="${show:0:$((c - 17))}…"
    w=$(( c - ${#show} - 16 ))
    while kill -0 "$CUR_PID" 2>/dev/null; do
      last=
      [ "$w" -gt 4 ] && last=$(tail -c 400 "$STEP_OUT" 2>/dev/null | tr '\r' '\n' | sed 's/\x1b\[[0-9;]*[A-Za-z]//g' | grep -v '^[[:space:]]*$' | tail -n1 | cut -c1-"$w" || true)
      printf '\r\e[K    %s%s%s %s %s%s %s%s' "$CYN" "${FRAMES[i % ${#FRAMES[@]}]}" "$R" "$show" "$D" "$(dur $((SECONDS-t)))" "$last" "$R"
      i=$((i+1)); sleep 0.1
    done
  fi
  wait "$CUR_PID" || rc=$?
  CUR_PID=
  [ "$FANCY" = 1 ] && [ "$VERBOSE" = 0 ] && printf '\r\e[K\e[?25h'
  [ "$VERBOSE" = 1 ] && sleep 0.1   # let tee finish
  cat "$STEP_OUT" >> "$LOG"
  if [ "$rc" = 0 ]; then
    echo "    ${OK} $label ${D}$(dur $((SECONDS-t)))${R}"
  else
    echo "    ${BAD} $label ${D}(exit $rc, $(dur $((SECONDS-t))))${R}"
    if [ "$VERBOSE" = 0 ]; then
      { tr '\r' '\n' < "$STEP_OUT" | grep -v '^[[:space:]]*$' | tail -n 8 | cut -c1-$(( $(cols) - 9 )) \
          | sed "s/^/      ${D}│${R} /"; } || true
    fi
    log "exit $rc"
  fi
  return "$rc"
}

# live "label" cmd...: interactive step (downloads with their own progress bar) - output goes straight to the terminal
live() {
  local label=$1; shift
  local t=$SECONDS rc=0
  echo "    ${CYN}›${R} $label"
  log "--- $label: $*"
  step_cmd "$@"
  "${STEP[@]}" &
  CUR_PID=$!
  wait "$CUR_PID" || rc=$?
  CUR_PID=
  if [ "$rc" = 0 ]; then echo "    ${OK} $label ${D}$(dur $((SECONDS-t)))${R}"
  else echo "    ${BAD} $label ${D}(exit $rc)${R}"; fi
  log "exit $rc"
  return "$rc"
}

stage_done() { log "stage time $(dur $((SECONDS-STAGE_T)))"; }

# ================================================================== steps =====
copy_scripts() {
  mkdir -p "$BIN" "$SHARE/webcrawler"
  local f
  for f in lchat-web lchat lchat-run lchat-models lchat-cards; do
    chmod +x "$REPO/bin/$f"
    if [ "$LINK" = 1 ]; then ln -sfn "$REPO/bin/$f" "$BIN/$f"; else install -m 755 "$REPO/bin/$f" "$BIN/$f"; fi
    echo "$f"
  done
  # modules lchat-web imports from its own folder (scheduler, issue watcher, recall entity graph)
  for f in lchat_sched.py lchat_watch.py lchat_graph.py; do
    if [ "$LINK" = 1 ]; then ln -sfn "$REPO/bin/$f" "$BIN/$f"; else install -m 644 "$REPO/bin/$f" "$BIN/$f"; fi
    echo "$f"
  done
  install -m 644 "$REPO/web/index.html" "$SHARE/index.html"
  install -m 644 "$REPO/vendor/webcrawler/webcrawler.py" "$SHARE/webcrawler/webcrawler.py"
}

copy_cards() {
  if [ "$LINK" = 1 ]; then
    rm -rf "$SHARE/cards" && ln -sfn "$REPO/cards" "$SHARE/cards"
  else   # replace the whole folder so cards deleted upstream disappear too
    rm -rf "$SHARE/cards.new" "$SHARE/cards" && mkdir -p "$SHARE/cards.new"
    (cd "$REPO/cards" && tar -cf - .) | (cd "$SHARE/cards.new" && tar -xf -)
    mv "$SHARE/cards.new" "$SHARE/cards"
  fi
}

ensure_pip() {   # SteamOS ships python without pip
  python3 -m pip --version >/dev/null 2>&1 && return 0
  python3 -m ensurepip --user 2>/dev/null && return 0
  local d rc=0; d=$(mktemp -d)
  curl -fsSL -o "$d/get-pip.py" https://bootstrap.pypa.io/get-pip.py && python3 "$d/get-pip.py" --user --break-system-packages || rc=$?
  rm -rf "$d"; return $rc
}

pip_install() {
  python3 -m pip install --user "$@" 2>/dev/null || python3 -m pip install --user --break-system-packages "$@"
}

fetch_llama() {
  local dir="$HOME/.local/share/llama.cpp" tmp src
  mkdir -p "$dir"
  tmp="$(mktemp -d)"
  curl -fL --progress-bar -o "$tmp/llama.tgz" \
    "https://github.com/ggml-org/llama.cpp/releases/download/$LLAMA_TAG/llama-$LLAMA_TAG-bin-ubuntu-vulkan-x64.tar.gz"
  mkdir -p "$tmp/x" && tar -xzf "$tmp/llama.tgz" -C "$tmp/x"
  src="$(find "$tmp/x" -name llama-server -type f | head -1)"
  [ -n "$src" ] || { echo "llama-server not found in the $LLAMA_TAG archive" >&2; rm -rf "$tmp"; return 1; }
  src="$(dirname "$src")"
  rm -rf "$dir/llama-$LLAMA_TAG" && mv "$src" "$dir/llama-$LLAMA_TAG" && rm -rf "$tmp"
  ln -sfn "llama-$LLAMA_TAG" "$dir/current"
  if [ -x "$dir/current/llama" ]; then ln -sfn "$dir/current/llama" "$BIN/llama"
  else ln -sfn "$dir/current/llama-server" "$BIN/llama-server"; fi
}

# KDE: let the screenshot tool capture one window without focusing it. KWin's ScreenShot2 API only answers programs
# whose .desktop file lists it, matched by executable path, so a private copy of the Python interpreter (a small stub
# that loads the system libpython) gets that entry and runs only bin/lchat_shot.py. No new power: spectacle can already
# capture the whole screen. Re-run install.sh after a Python upgrade; until then lchat falls back to focusing the window.
shot_helper() {
  install -m 644 "$REPO/bin/lchat_shot.py" "$SHARE/lchat_shot.py"
  install -m 755 "$(readlink -f "$(command -v python3)")" "$SHARE/lchat-shot"
  mkdir -p "$HOME/.local/share/applications"
  cat > "$HOME/.local/share/applications/lchat-shot.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=lchat window capture
Comment=Lets lchat's screenshot tool capture one window without focusing it (KWin ScreenShot2)
Exec=$SHARE/lchat-shot
NoDisplay=true
X-KDE-DBUS-Restricted-Interfaces=org.kde.KWin.ScreenShot2
EOF
  kbuildsycoca6 >/dev/null 2>&1 || true
}

# first install: builds ~/.local/state/lchat/cards.db; update: re-embeds only new/changed cards.
# Vectors need the embedding server (lchat-run starts it); if it is down, lchat-web embeds the cards when it starts.
index_cards() { LCHAT_CARDS="$SHARE/cards" python3 "$BIN/lchat-cards" sync; }

# =================================================================== main =====
export REPO BIN SHARE LINK LLAMA_TAG
export -f copy_scripts copy_cards ensure_pip pip_install fetch_llama shot_helper index_cards
mkdir -p "$STATE"
{ echo "lchat install $(date '+%Y-%m-%dT%H:%M:%S%z')  args: $*"; echo "repo $REPO"; } > "$LOG"
banner

stage "${STAGES[0]}"
command -v python3 >/dev/null || { echo "    ${BAD} python3 not found - install Python 3.10+ first"; exit 1; }
PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
if python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then ok "python $PYV"; else warn "python $PYV is old - 3.10+ is tested"; fi
command -v curl >/dev/null && ok "curl" || warn "curl not found - downloads (models, llama.cpp, pip bootstrap) will fail"
FREE_KB=$(df -Pk "$HOME" | awk 'NR==2 {print $4}')
FREE_GB=$(( FREE_KB / 1024 / 1024 ))
NEED=1; [ "$MODELS" = 1 ] && NEED=8
if [ "$FREE_GB" -lt "$NEED" ]; then warn "only ${FREE_GB} GiB free in $HOME (want ~${NEED} GiB)"; else ok "${FREE_GB} GiB free in $(tidy "$HOME")/"; fi
GPU=$(command -v vulkaninfo >/dev/null && timeout 5 vulkaninfo --summary 2>/dev/null | awk -F= '/deviceName/ {gsub(/^ +/, "", $2); print $2; exit}' || true)
[ -n "$GPU" ] && info "GPU: $GPU"
stage_done

stage "${STAGES[1]}"
run "scripts -> $(tidy "$BIN")" copy_scripts
info "$(wc -l < "$STEP_OUT") files, page + webcrawler -> $(tidy "$SHARE")"
case ":$PATH:" in *":$BIN:"*) ;; *) info "$(tidy "$BIN") is not on PATH - add it to your shell profile";; esac
stage_done

stage "${STAGES[2]}"
run "cards -> $(tidy "$SHARE/cards")" copy_cards
info "$(find "$SHARE/cards/" -name '*.json' ! -name card.schema.json | wc -l) cards"
stage_done

stage "${STAGES[3]}"
PKGS=() HAVE=()
for m in numpy:numpy PIL:pillow httpx:httpx cloakbrowser:cloakbrowser; do
  if python3 -c "import ${m%%:*}" 2>/dev/null; then HAVE+=("${m#*:}"); else PKGS+=("${m#*:}"); fi
done
[ ${#HAVE[@]} -gt 0 ] && ok "present: ${HAVE[*]}"
if [ ${#PKGS[@]} -gt 0 ]; then
  if run "pip" ensure_pip && run "pip install ${PKGS[*]}" pip_install "${PKGS[@]}"; then :; else
    fail "pip install ${PKGS[*]} - web_search/history need them"; fi
fi
stage_done

stage "${STAGES[4]}"
if command -v llama >/dev/null || command -v llama-server >/dev/null; then
  ok "found $(command -v llama || command -v llama-server)"
elif [ "$WITH_LLAMA" = 1 ]; then
  live "llama.cpp $LLAMA_TAG (Vulkan x64)" fetch_llama || fail "llama.cpp download - rerun with --with-llama"
else
  warn "llama.cpp not found - rerun with --with-llama, or set LLAMA=/path/to/llama-server"
fi
stage_done

stage "${STAGES[5]}"
if command -v kbuildsycoca6 >/dev/null && command -v spectacle >/dev/null; then
  run "KWin ScreenShot2 helper" shot_helper || fail "window capture helper - lchat will focus windows to capture them"
else
  skip "not KDE Plasma 6 with spectacle"
fi
MISSING=()
for t in pdftotext spectacle qdbus; do command -v "$t" >/dev/null || MISSING+=("$t"); done
[ ${#MISSING[@]} -gt 0 ] && info "not found: ${MISSING[*]} - the tools that use them will report an error"
stage_done

stage "${STAGES[6]}"
if [ "$MODELS" = 1 ]; then
  live "models (resumable)" "$BIN/lchat-models" download || fail "model download incomplete - run: lchat-models download"
else
  skip "--no-models (run lchat-models download later)"
fi
stage_done

stage "${STAGES[7]}"
if run "lchat-cards sync" index_cards; then
  SUM=$(grep -E '^cards:' "$STEP_OUT" | sed -E 's/.*\(version ([^)]*)\): ([^>]*) ->.*/\2 (version \1)/' | head -1 || true)
  [ -n "$SUM" ] && info "$SUM"
  EMB=$(grep -E '^embedded|^note:' "$STEP_OUT" | head -1 || true)
  [ -n "$EMB" ] && info "$EMB"
else
  fail "help-card index - run: lchat-cards sync"
fi
stage_done

# ================================================================ summary =====
echo
[ "$FANCY" = 1 ] && { echo "  $(bar "$NSTAGES")"; echo; }
if [ ${#FAILED[@]} -gt 0 ]; then
  echo "  ${BAD} ${B}lchat installed, but ${#FAILED[@]} step(s) failed${R} in $(dur $((SECONDS-T0)))"
  for w in "${FAILED[@]}"; do echo "     ${RED}•${R} $w"; done
  for w in "${WARNINGS[@]}"; do echo "     ${YEL}•${R} $w"; done
elif [ ${#WARNINGS[@]} -eq 0 ]; then
  echo "  ${OK} ${B}lchat installed${R} in $(dur $((SECONDS-T0)))"
else
  echo "  ${WRN} ${B}lchat installed with ${#WARNINGS[@]} warning(s)${R} in $(dur $((SECONDS-T0)))"
  for w in "${WARNINGS[@]}"; do echo "     ${YEL}•${R} $w"; done
fi
echo
echo "  ${B}next${R}"
echo "     lchat-run            ${D}start llama.cpp + the web page${R}"
echo "     lchat-run stop       ${D}stop everything${R}"
echo "     lchat-run restart    ${D}after an update${R}"
echo "     ${D}open http://127.0.0.1:8088/  ·  log: $(tidy "$LOG")${R}"
echo
log "done in $(dur $((SECONDS-T0))), ${#WARNINGS[@]} warning(s), ${#FAILED[@]} failed step(s)"
FINISHED=1
[ ${#FAILED[@]} -eq 0 ] || exit 1
