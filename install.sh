#!/usr/bin/env bash
# lchat installer: scripts -> ~/.local/bin, page + webcrawler + help cards -> ~/.local/share/lchat,
# Python deps (pip --user), optional llama.cpp Vulkan build, then the models, then the help-card index.
# Re-run it after `git pull` to update: changed cards are re-indexed, removed cards are dropped.
#
#   ./install.sh                 install + download models
#   ./install.sh --link          symlink scripts to this checkout instead of copying (for development)
#   ./install.sh --with-llama    also install a prebuilt llama.cpp (Vulkan, x64) if `llama` is missing
#   ./install.sh --no-models     skip the model download (run `lchat-models` later)
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
BIN="$HOME/.local/bin"
SHARE="$HOME/.local/share/lchat"
LLAMA_TAG="${LLAMA_TAG:-b12290}"
LINK=0 WITH_LLAMA=0 MODELS=1
for arg in "$@"; do
  case "$arg" in
    --link) LINK=1 ;;
    --with-llama) WITH_LLAMA=1 ;;
    --no-models) MODELS=0 ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }

say "scripts -> $BIN"
mkdir -p "$BIN" "$SHARE/webcrawler"
for f in lchat-web lchat lchat-run lchat-models lchat-cards; do
  chmod +x "$REPO/bin/$f"
  if [ "$LINK" = 1 ]; then ln -sfn "$REPO/bin/$f" "$BIN/$f"; else install -m 755 "$REPO/bin/$f" "$BIN/$f"; fi
done
# modules lchat-web imports from its own folder (scheduler, issue watcher)
for f in lchat_sched.py lchat_watch.py; do
  if [ "$LINK" = 1 ]; then ln -sfn "$REPO/bin/$f" "$BIN/$f"; else install -m 644 "$REPO/bin/$f" "$BIN/$f"; fi
done
install -m 644 "$REPO/web/index.html" "$SHARE/index.html"
install -m 644 "$REPO/vendor/webcrawler/webcrawler.py" "$SHARE/webcrawler/webcrawler.py"

say "help cards -> $SHARE/cards"
if [ "$LINK" = 1 ]; then
  rm -rf "$SHARE/cards" && ln -sfn "$REPO/cards" "$SHARE/cards"
else   # replace the whole folder so cards deleted upstream disappear too
  rm -rf "$SHARE/cards.new" "$SHARE/cards" && mkdir -p "$SHARE/cards.new"
  (cd "$REPO/cards" && tar -cf - .) | (cd "$SHARE/cards.new" && tar -xf -)
  mv "$SHARE/cards.new" "$SHARE/cards"
fi
echo "$(find "$SHARE/cards" -name '*.json' ! -name card.schema.json | wc -l) cards"

say "python packages"
PKGS=()
python3 -c 'import numpy' 2>/dev/null || PKGS+=(numpy)
python3 -c 'import PIL' 2>/dev/null || PKGS+=(pillow)
python3 -c 'import httpx' 2>/dev/null || PKGS+=(httpx)
python3 -c 'import cloakbrowser' 2>/dev/null || PKGS+=(cloakbrowser)
if [ ${#PKGS[@]} -gt 0 ]; then
  if ! python3 -m pip --version >/dev/null 2>&1; then   # SteamOS ships python without pip
    python3 -m ensurepip --user 2>/dev/null || {
      curl -fsSL -o /tmp/get-pip.$$.py https://bootstrap.pypa.io/get-pip.py
      python3 /tmp/get-pip.$$.py --user --break-system-packages; rm -f /tmp/get-pip.$$.py; }
  fi
  python3 -m pip install --user "${PKGS[@]}" 2>/dev/null \
    || python3 -m pip install --user --break-system-packages "${PKGS[@]}" \
    || echo "WARNING: pip install ${PKGS[*]} failed - web_search/history need them" >&2
else
  echo "all present"
fi

if ! command -v llama >/dev/null && ! command -v llama-server >/dev/null; then
  if [ "$WITH_LLAMA" = 1 ]; then
    say "llama.cpp $LLAMA_TAG (Vulkan)"
    dir="$HOME/.local/share/llama.cpp"
    mkdir -p "$dir"
    tmp="$(mktemp -d)"
    curl -fL --progress-bar -o "$tmp/llama.tgz" \
      "https://github.com/ggml-org/llama.cpp/releases/download/$LLAMA_TAG/llama-$LLAMA_TAG-bin-ubuntu-vulkan-x64.tar.gz"
    mkdir -p "$tmp/x" && tar -xzf "$tmp/llama.tgz" -C "$tmp/x"
    src="$(dirname "$(find "$tmp/x" -name llama-server -type f | head -1)")"
    rm -rf "$dir/llama-$LLAMA_TAG" && mv "$src" "$dir/llama-$LLAMA_TAG" && rm -rf "$tmp"
    ln -sfn "llama-$LLAMA_TAG" "$dir/current"
    if [ -x "$dir/current/llama" ]; then ln -sfn "$dir/current/llama" "$BIN/llama"
    else ln -sfn "$dir/current/llama-server" "$BIN/llama-server"; fi
  else
    echo "WARNING: llama.cpp not found - rerun with --with-llama, or set LLAMA=/path/to/llama-server" >&2
  fi
fi

# KDE: let the screenshot tool capture one window without focusing it. KWin's ScreenShot2 API only answers programs
# whose .desktop file lists it, matched by executable path, so a private copy of the Python interpreter (a small stub
# that loads the system libpython) gets that entry and runs only bin/lchat_shot.py. No new power: spectacle can already
# capture the whole screen. Re-run install.sh after a Python upgrade; until then lchat falls back to focusing the window.
if command -v kbuildsycoca6 >/dev/null && command -v spectacle >/dev/null; then
  say "window capture helper (KWin ScreenShot2)"
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
fi

for t in pdftotext spectacle qdbus; do
  command -v "$t" >/dev/null || echo "note: $t not found - the tools that use it will report an error"
done
case ":$PATH:" in *":$BIN:"*) ;; *) echo "note: add $BIN to PATH" ;; esac

if [ "$MODELS" = 1 ]; then say "models"; "$BIN/lchat-models" download; fi

# first install: builds ~/.local/state/lchat/cards.db; update: re-embeds only new/changed cards.
# Vectors need the embedding server (lchat-run starts it); if it is down, lchat-web embeds the cards when it starts.
say "help-card index"
LCHAT_CARDS="$SHARE/cards" python3 "$BIN/lchat-cards" sync || echo "WARNING: lchat-cards sync failed - run: lchat-cards sync" >&2

say "done - start everything with: lchat-run   (stop: lchat-run stop)"
