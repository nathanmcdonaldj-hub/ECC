#!/bin/bash
# Nightly clipping run. Started by launchd (com.openshorts.night) at 22:00.
# Processes queued videos in ~/Movies/Clip Inbox and URLs in links.txt,
# one at a time. Never posts or schedules anything.
# Never runs in the daytime: launchd replays missed jobs on wake, so check the clock.
# Dry run: night_run.sh --dry-run  (checks only, starts nothing heavy)
INBOX="$HOME/Movies/Clip Inbox"
LINKS="$INBOX/links.txt"
REPO="$HOME/openshorts"
DIR="$REPO/autopilot"
DONE="$DIR/processed.txt"
LOCK="$DIR/.night.lock"
PY=/usr/bin/python3
DOCKER=/Applications/Docker.app/Contents/Resources/bin/docker
export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"
DRY=0; [ "$1" = "--dry-run" ] && DRY=1
ts() { date "+%Y-%m-%d %H:%M:%S"; }
mins() { echo $((10#$(date +%H) * 60 + 10#$(date +%M))); }
in_window() { [ "$AUTOPILOT_FORCE" = "1" ] && return 0; local m; m=$(mins); [ "$m" -ge 1320 ] || [ "$m" -lt 330 ]; }   # 22:00–05:30
may_start() { [ "$AUTOPILOT_FORCE" = "1" ] && return 0; local m; m=$(mins); [ "$m" -ge 1320 ] || [ "$m" -lt 300 ]; }   # 22:00–05:00
notify() { osascript -e "display notification \"${1//\"/\'}\" with title \"OpenShorts\" sound name \"Glass\""; }
touch "$DONE"
# Redo requests from the Stratus House Media dashboard (~/Movies/Clip Inbox/redo.json).
# Simple trims only, host ffmpeg at low priority; anything else stays in redo.json.
redone=0
run_redo() {
  [ -s "$INBOX/redo.json" ] || return 0
  may_start || { echo "$(ts) past 05:00, redo requests left for tomorrow"; return 0; }
  local out; out=$("$PY" -I "$DIR/redo_clips.py" $1 2>&1)
  echo "$out"
  redone=$(echo "$out" | sed -n 's/^REDO_DONE //p'); redone=${redone:-0}
}

echo "$(ts) night run invoked (dry=$DRY)"
if ! in_window; then
  echo "$(ts) outside 22:00–05:30, exiting without doing anything"; exit 0
fi

if ! mkdir "$LOCK" 2>/dev/null; then
  pid=$(cat "$LOCK/pid" 2>/dev/null)
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then echo "$(ts) already running"; exit 0; fi
  rm -rf "$LOCK"; mkdir "$LOCK" || exit 0
fi
echo $$ > "$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT

# Build the queue: videos (name|size) and links, skipping ones already done.
queue=()
while IFS= read -r -d '' f; do
  id="$(basename "$f")|$(stat -f %z "$f")"
  grep -qxF "$id" "$DONE" || queue+=("$f")
done < <(find "$INBOX" -maxdepth 1 -type f ! -name '.*' \( -iname '*.mp4' -o -iname '*.mov' \
          -o -iname '*.m4v' -o -iname '*.mkv' -o -iname '*.webm' -o -iname '*.avi' \) -print0 | sort -z)
if [ -f "$LINKS" ]; then
  while IFS= read -r u || [ -n "$u" ]; do
    u="$(echo "$u" | tr -d '\r' | xargs)"
    case "$u" in http://*|https://*) grep -qxF "link|$u" "$DONE" || queue+=("$u");; esac
  done < "$LINKS"
fi
echo "$(ts) queue: ${#queue[@]} item(s)"
if [ ${#queue[@]} -eq 0 ]; then
  if [ $DRY -eq 1 ]; then run_redo --dry-run; else run_redo; fi
  [ "$redone" -gt 0 ] && notify "Night run: $redone clip redo(s) ready in Movies/Clips Ready."
  echo "$(ts) nothing else to do"; exit 0
fi
if [ $DRY -eq 1 ]; then
  for q in "${queue[@]}"; do echo "  would process: $q"; done
  run_redo --dry-run
  notify "Night run dry run OK: ${#queue[@]} item(s) queued. Nothing started."; exit 0
fi

# Keep the Mac awake (not the display) while this script runs.
caffeinate -i -w $$ &

open -g -j -a Docker   # -g: stay in background, -j: launch hidden (no Docker Desktop window)
for i in $(seq 1 60); do "$DOCKER" info >/dev/null 2>&1 && break; sleep 5; done
"$DOCKER" info >/dev/null 2>&1 || { echo "$(ts) Docker did not start"; notify "Night run failed: Docker did not start."; exit 1; }
# Interrupted jobs must not auto-resume (their key is gone; they would just fail).
find "$REPO/output" -maxdepth 2 -name .resume.json -delete 2>/dev/null
(cd "$REPO" && "$DOCKER" compose up -d) 2>&1 | tail -3
for i in $(seq 1 60); do curl -fs -o /dev/null --max-time 5 http://localhost:8000/health/ready && break; sleep 5; done
curl -fs -o /dev/null --max-time 5 http://localhost:8000/health/ready || { echo "$(ts) backend not healthy"; notify "Night run failed: OpenShorts did not start."; exit 1; }

ok=0; failed=0; saved=()
for q in "${queue[@]}"; do
  if ! may_start; then echo "$(ts) past 05:00, not starting more"; break; fi
  if [[ "$q" == http* ]]; then id="link|$q"; label="$q"; else id="$(basename "$q")|$(stat -f %z "$q")"; label="$(basename "$q")"; fi
  echo "$(ts) processing: $label"
  out=$("$PY" -I "$DIR/process_video.py" "$q" --quiet 2>&1); rc=$?
  echo "$out"
  # Only mark done when it finished (ok or a real failure), not when 06:00 killed it.
  if [ $rc -ge 128 ]; then echo "$(ts) interrupted: $label (will retry next night)"; break; fi
  # Stratus House "Hide the streamer": 75 = hiding failed; keep it queued, never clip it unhidden.
  if [ $rc -eq 75 ]; then failed=$((failed+1)); echo "$(ts) HIDE failed, left for retry: $label"; continue; fi
  echo "$id" >> "$DONE"
  if [ $rc -eq 0 ]; then ok=$((ok+1)); saved+=("$(echo "$out" | sed -n 's/^SAVED //p' | xargs basename)")
  else failed=$((failed+1)); echo "$(ts) FAILED: $label"; fi
done

run_redo
left=$(( ${#queue[@]} - ok - failed ))
msg="Night run done: $ok video(s) clipped"
[ $failed -gt 0 ] && msg="$msg, $failed failed"
[ $left -gt 0 ] && msg="$msg, $left left for tomorrow"
[ "$redone" -gt 0 ] && msg="$msg, $redone redo(s) made"
[ $ok -gt 0 ] && msg="$msg. Clips in Movies/Clips Ready. Message Claude."
echo "$(ts) $msg"
notify "$msg"
