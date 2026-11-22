#!/bin/bash
# Run VoiceStudio's text-to-speech backend without its window.
#   voicestudio-backend.sh start [--wait] | stop | status
# "start" launches it in the background and returns at once (the caller polls
# http://127.0.0.1:3900/health); "start --wait" waits until it answers (max 3 min).
# Uses the Python runtime VoiceStudio installed on first open. Listens only on
# 127.0.0.1:3900 at low priority. Log: ~/Library/Logs/VoiceStudio-backend.log
# Speech API: POST http://127.0.0.1:3900/v1/audio/speech  (OpenAI-compatible)
# Safe to call from a LaunchAgent (minimal PATH): sets its own PATH and HOME fallback.
export PATH="/usr/bin:/bin:/usr/sbin:/sbin"
[ -n "$HOME" ] || HOME="$(/usr/bin/dscl . -read "/Users/$(/usr/bin/id -un)" NFSHomeDirectory | awk '{print $2}')"
export HOME
RT="$HOME/Library/Application Support/VoiceStudio/runtime/project"
PY="$RT/.venv/bin/python"
PORT=3900
LOG="$HOME/Library/Logs/VoiceStudio-backend.log"
PIDF="$HOME/Library/Application Support/VoiceStudio/backend-headless.pid"

running() { curl -sf -o /dev/null --max-time 3 "http://127.0.0.1:$PORT/health"; }
listener() { lsof -tiTCP:$PORT -sTCP:LISTEN 2>/dev/null | head -1; }

case "$1" in
  start)
    if running; then echo "already running on 127.0.0.1:$PORT"; exit 0; fi
    pid=$(cat "$PIDF" 2>/dev/null)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then echo "already starting (pid $pid)"; exit 0; fi
    [ -x "$PY" ] || { echo "VoiceStudio runtime not found: open the VoiceStudio app once first"; exit 1; }
    mkdir -p "$HOME/Library/Logs"
    cd "$RT" || exit 1
    # Detach fully: no inherited stdin/stdout/stderr, so a caller that captures output never blocks.
    OMNIVOICE_PORT=$PORT nohup nice -n 10 "$PY" -m uvicorn main:app --app-dir backend \
      --host 127.0.0.1 --port $PORT </dev/null >> "$LOG" 2>&1 &
    echo $! > "$PIDF"
    if [ "$2" = "--wait" ]; then
      for i in $(seq 1 60); do running && { echo "started on 127.0.0.1:$PORT (pid $(cat "$PIDF"))"; exit 0; }; sleep 3; done
      echo "did not answer within 3 minutes, see $LOG"; exit 1
    fi
    echo "starting (pid $(cat "$PIDF")); poll http://127.0.0.1:$PORT/health" ;;
  stop)
    pid=$(cat "$PIDF" 2>/dev/null)
    { [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; } || pid=$(listener)
    # Never stop the backend the VoiceStudio app itself runs (its parent is the app).
    if [ -n "$pid" ] && ps -o args= -p "$(ps -o ppid= -p "$pid" | tr -d ' ')" 2>/dev/null | grep -q "VoiceStudio.app/Contents/MacOS"; then
      echo "the VoiceStudio app is running its own backend; leaving it alone"; rm -f "$PIDF"; exit 0
    fi
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      kill -TERM "$pid"; for i in $(seq 1 15); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
      kill -0 "$pid" 2>/dev/null && kill -KILL "$pid"
      echo "stopped"
    else
      echo "not running"
    fi
    rm -f "$PIDF" ;;
  status)
    if running; then curl -s --max-time 3 "http://127.0.0.1:$PORT/health"; echo; else echo "not running"; fi ;;
  *) echo "usage: $0 start [--wait] | stop | status"; exit 2 ;;
esac
