#!/bin/bash
# Install the Stratus House Media dashboard on this Mac.
# - copies the app to ~/stratus-media
# - starts it at login (LaunchAgent, low priority, restarts if it stops)
# - adds "Stratus House Media" to ~/Applications that opens the dashboard
# Safe to run again: it updates in place and keeps your data and keys.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/stratus-media"
LABEL="com.stratushouse.media"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PORT="${STRATUS_PORT:-5190}"
PY="/usr/bin/python3"

if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
  echo "[StratusMedia] Python 3.9+ is needed (comes with Apple's command line tools)." >&2
  exit 1
fi

mkdir -p "$DEST/static/employees" "$DEST/bin" "$HOME/Library/LaunchAgents" \
  "$HOME/Library/Application Support/StratusMedia" "$HOME/Library/Logs"
if [ "$SRC" != "$DEST" ]; then
  cp "$SRC/server.py" "$DEST/server.py"
  cp "$SRC/static/index.html" "$DEST/static/index.html"
  cp "$SRC"/static/employees/*.jpg "$DEST/static/employees/" 2>/dev/null || true
  cp "$SRC/install.sh" "$DEST/install.sh"
fi
chmod +x "$DEST/install.sh"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$PY</string><string>$DEST/server.py</string></array>
  <key>EnvironmentVariables</key>
  <dict><key>STRATUS_PORT</key><string>$PORT</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>Nice</key><integer>10</integer>
  <key>LowPriorityIO</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/StratusMedia.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/StratusMedia.log</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

# A clickable app that opens the dashboard in the default browser.
APP="$HOME/Applications/Stratus House Media.app"
mkdir -p "$HOME/Applications"
rm -rf "$APP"
osacompile -o "$APP" -e "open location \"http://127.0.0.1:$PORT\"" >/dev/null

for _ in 1 2 3 4 5 6 7 8 9 10; do
  if curl -fsS "http://127.0.0.1:$PORT/api/state" -o /dev/null 2>/dev/null; then
    echo "[StratusMedia] Running at http://127.0.0.1:$PORT"
    exit 0
  fi
  sleep 1
done
echo "[StratusMedia] Installed, but it didn't answer yet. See ~/Library/Logs/StratusMedia.log" >&2
exit 1
