#!/usr/bin/env bash
# install.sh hub|satellite — install the launchd job for this machine's role.
set -euo pipefail
ROLE="${1:-}"; [ "$ROLE" = hub ] || [ "$ROLE" = satellite ] || { echo "usage: bin/install.sh hub|satellite"; exit 2; }
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.commandboard.command-board"; [ "$ROLE" = satellite ] && LABEL="$LABEL-satellite"
SRC="$REPO/launchd/$LABEL.plist"; DST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs/command-board"
sed "s|__HOME__|$HOME|g" "$SRC" > "$DST"
chmod +x "$REPO/bin/command-board" "$REPO/bin/satellite-sync" "$REPO/bin/"*.py "$REPO/watchers/"*.py 2>/dev/null || true
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$DST"
launchctl print "gui/$(id -u)/$LABEL" | grep -E "state|program" | head -3
echo "installed $LABEL from $DST"
