#!/usr/bin/env bash
# One-line install or update for zombiePlot. On the Raspberry Pi, run:
#
#   curl -fsSL https://raw.githubusercontent.com/mikedero08/zombie-plot/main/get.sh | sudo bash
#
# It downloads the newest release and runs its install.sh.

set -euo pipefail

REPO="${ZOMBIEPLOT_REPO:-mikedero08/zombie-plot}"
RAW="${ZOMBIEPLOT_RAW_BASE:-https://raw.githubusercontent.com}/$REPO"

[ "$(id -u)" -eq 0 ] || { echo "Run it with sudo:  curl -fsSL $RAW/main/get.sh | sudo bash" >&2; exit 1; }
command -v python3 >/dev/null || { apt-get update && apt-get install -y python3; }

echo "Finding the newest zombiePlot release..."
TAG="$(curl -fsSL "$RAW/main/latest.json" | python3 -c 'import json, sys; print(json.load(sys.stdin)["tag"])')" \
    || { echo "Could not read the release information from GitHub. Check this Pi is online." >&2; exit 1; }
case "$TAG" in *[!A-Za-z0-9._-]*|"") echo "Unexpected release name: $TAG" >&2; exit 1 ;; esac

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
for f in zombieplot.py install.sh; do
    curl -fsSL -o "$TMP/$f" "$RAW/$TAG/$f" || { echo "Download of $f failed." >&2; exit 1; }
done
echo "Downloaded $TAG."

cd "$TMP"
# Answers to the installer's questions come from the keyboard, not from this pipe.
if { : < /dev/tty; } 2>/dev/null; then
    bash install.sh "$@" < /dev/tty
else
    bash install.sh --upgrade
fi
