"""OpenShorts Clip Inbox autopilot: clip ONE local video via the local API.

Usage: python3 -I process_video.py <video_path_or_url> [--dry-run] [--quiet]

- Settings: 9:16, burned captions, auto hook, clips under 60 s.
- Saves the top 3 clips (by predicted_score) + a 1 s thumbnail each into
  ~/Movies/Clips Ready/<video name>/ with a clips.md summary.
- NEVER posts or schedules anything: it only calls upload, process and status.
- Reads the Gemini key from ~/openshorts/autopilot-keys.env and never prints it.
"""
import json, math, os, re, shutil, struct, subprocess, sys, time, urllib.error, urllib.request

API = "http://localhost:8000"
HOME = os.path.expanduser("~")
REPO = os.path.join(HOME, "openshorts")
OUT_ROOT = os.path.join(HOME, "Movies", "Clips Ready")
DOCKER = "/Applications/Docker.app/Contents/Resources/bin/docker"
BACKEND = "openshorts-backend"
TOP_N = 3
# Only these endpoints are ever called. Nothing social/publish/schedule.
ALLOWED = ("/api/uploads", "/api/process", "/api/status/", "/health/ready")

# --- Stratus House "Hide the streamer" (added 2026-11-20) -------------------
# The dashboard flags sources in ~/Movies/Clip Inbox/hide.json
# ({"links": [url], "files": [name]}). For a flagged source:
#   pre-step : cover the facecam (stratus-hide image) BEFORE OpenShorts sees it,
#              and ask OpenShorts for the plain crop (layouts=["none"], no camera band)
#   post-step: strip the voice from each finished clip, delete media that still
#              has the voice, write hidden.txt
# Fail-safe: a flagged video is never clipped unhidden.
HIDE_FILE = os.path.join(HOME, "Movies", "Clip Inbox", "hide.json")
HIDE_IMAGE = "stratus-hide"
HIDE_TOOL = "/opt/stratus/hide_streamer.py"
HIDE_WORK = os.path.join(REPO, "output", "_hide")
HOST_FFMPEG = os.path.join(HOME, "stratus-media", "bin", "ffmpeg")
EXIT_RETRY = 75   # night_run.sh: hide failed, leave it in the queue for the next run

# --- Stratus House "Game play only" (added 2026-11-20) ----------------------
# Flagged in ~/Movies/Clip Inbox/gameplay.json (same format as hide.json). OpenShorts is asked
# for more candidates; each candidate's frames are checked by Gemini (same key) and only clips
# that show the game itself are kept (no tweets, web pages, desktop, just-chatting).
GAME_FILE = os.path.join(HOME, "Movies", "Clip Inbox", "gameplay.json")
GAME_CANDIDATES = 8
GEMINI_MODEL = os.environ.get("STRATUS_GEMINI_MODEL", "gemini-3.1-flash-lite")
GAME_PROMPT = ("These are {n} frames from one short clip of a livestream. For EACH frame decide if it mainly shows "
               "video game play (the game world, game HUD, cutscene or game menu). NOT game play: a desktop or file "
               "window, a web page, social media posts or tweets, a chat window, a video player showing something "
               "else, or the streamer's camera filling most of the frame. Reply with JSON only: "
               "{{\"gameplay\": [true/false for each frame in order]}}")


# --- Stratus House "Big moments only" (added 2026-11-26) --------------------
# For podcasts, debates, interviews and other conversation videos, only the BIG moments are kept:
# gotcha / mic-drop points, someone caught contradicting themselves, heated back-and-forths,
# big laughs, shocking reveals. Gemini (same key) listens to each candidate's audio and looks at
# 4 frames. If the video is a conversation and no candidate is a big moment, NO clips are made and
# the log says "BIGMOMENT: no big moment found" (the dashboard tells Mr. McDonald).
# Videos that aren't conversations (sports, vlogs, gameplay) keep the normal top 3.
TALK_CANDIDATES = 6
BIG_MIN_SCORE = 7
BIG_HINT = ("Pick only the BIG moments: a gotcha or mic-drop point where one person makes the other look foolish, "
            "someone caught contradicting themselves, a heated back-and-forth, a big laugh, or a shocking reveal. "
            "Skip small talk, intros, ads and calm explaining.")
BIG_PROMPT = ("You get the audio of one short clip (under 60 s) and {n} frames from it. Reply with JSON only: "
              "{{\"conversation\": true if it is mainly people talking (podcast, debate, interview, panel, "
              "conversation, call-in show), \"big\": true only if it is a BIG moment: a gotcha or mic-drop point that "
              "makes the other person look foolish, someone caught contradicting themselves, a heated back-and-forth, "
              "a big laugh, or a shocking reveal. Plain chit-chat, intros, ads, slow explaining, calm agreement "
              "and one person just telling a story are NOT big. \"score\": 1-10 how strong the moment is, \"kind\": short label like 'gotcha', "
              "'heated', 'big laugh', 'contradiction', 'reveal' or 'none', \"why\": one short sentence}}")
# If OpenShorts' /api/process takes a free-text focus for picking moments, set its field name here.
OPENSHORTS_HINT_FIELD = None

# --- Stratus House "Reactor" (added 2026-11-27) ------------------------------
# The dashboard notes the reactor picked for a source in ~/Movies/Clip Inbox/reactor.json
# ({"links": {url: character}, "files": {name: character}}). Each finished clip (captions already
# burned in) then opens with the reactor saying "Check this out" in Mr. McDonald's own voice and
# keeps him in a lower-left corner box while the clip plays. Music and the end tag are added later by the
# dashboard. Fail-open: if the step can't run, the plain clip is kept and the log says why.
REACTOR_FILE = os.path.join(HOME, "Movies", "Clip Inbox", "reactor.json")
REACTOR_DIR = os.path.join(HOME, "stratus-media", "tools", "reactor")
# Newest installed template wins. reactor3 = reacts to what is said (asks Gemini for the moments from
# the clip's audio, falls back to a calm idle); reactor2 = calm idle only. His voice is baked into the intro.
REACTOR_TOOLS = ("reactor3.py", "reactor2.py")
REACTOR_MANIFEST = os.path.join(REACTOR_DIR, "reactors.json")  # [{"id", "name", ...}]: the characters he approved
REACTOR_ROOM = 2   # seconds kept free for the ~1.5 s intro, so intro + clip stays under 60 s

# --- Stratus House "Words above the reactor" (added 2026-11-27) --------------
# A template whose box sits at the bottom of the frame says so per character in reactors.json:
#   "caption_bottom": the row (pixels of a 1920-tall frame) the burned-in words must end at
#   "room":           seconds the template adds to a clip in total (intro + outro)
# OpenShorts has no caption-height option, but every job keeps the clean clip, the exact .ass it
# burned and the hook picture + position. So for those characters the clip is burned again from
# these with the words higher, inside the backend container (the same ffmpeg, libass and fonts
# OpenShorts used), and the reactor is added to that. OpenShorts itself is not changed.
# No reactor picked, or a character without "caption_bottom": clips are exactly as before.
# Fail-open: if the words can't be moved, the plain clip is kept (no reactor on top of the words).
CAPTION_GAP = 0.116      # the lowest ink (commas, pop effect) ends this fraction of the font size above the style's bottom margin
CAPTION_BAND = 200       # rows the words can fill above "caption_bottom" (one line, pop effect included)
CONTAINER_FONTS = "/app/fonts"
ASS_STYLE_FIELDS = ["name", "fontname", "fontsize", "primarycolour", "secondarycolour", "outlinecolour",
                    "backcolour", "bold", "italic", "underline", "strikeout", "scalex", "scaley", "spacing",
                    "angle", "borderstyle", "outline", "shadow", "alignment", "marginl", "marginr", "marginv",
                    "encoding"]


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def read_key():
    with open(os.path.join(REPO, "autopilot-keys.env"), encoding="utf-8") as f:
        for line in f:
            if line.startswith("GEMINI_API_KEY="):
                return line.split("=", 1)[1].strip()
    return ""


class Api:
    def __init__(self, key):
        self.key = key

    def call(self, method, path, data=None, headers=None, timeout=60):
        if not path.startswith(ALLOWED):
            raise RuntimeError(f"refusing to call {path}")
        h = {"X-Gemini-Key": self.key} if self.key else {}
        h.update(headers or {})
        req = urllib.request.Request(API + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{method} {path} -> HTTP {e.code}: "
                               f"{e.read().decode(errors='replace')[:600]}")


def hide_flagged(video, is_url, path=HIDE_FILE):
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return False
    if is_url:
        return video.strip() in [str(u).strip() for u in (d.get("links") or [])]
    return os.path.basename(video) in [os.path.basename(str(f)) for f in (d.get("files") or [])]


def reactor_ids(path=None):
    """Character ids the installed reactor template offers."""
    try:
        with open(path or REACTOR_MANIFEST, encoding="utf-8") as fh:
            return {str(r.get("id")) for r in json.load(fh)
                    if isinstance(r, dict) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", str(r.get("id") or ""))}
    except (OSError, ValueError, TypeError, AttributeError):
        return set()


def reactor_pick(video, is_url, path=None):
    """The reactor character picked for this source on the dashboard, or None."""
    try:
        with open(path or REACTOR_FILE, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return None
    if is_url:
        c = (d.get("links") or {}).get(video.strip())
    else:
        c = {os.path.basename(str(k)): v for k, v in (d.get("files") or {}).items()}.get(os.path.basename(video))
    return c if c in reactor_ids() else None


def reactor_info(character, path=None):
    """The installed template's entry for one character ({} when there is none)."""
    try:
        with open(path or REACTOR_MANIFEST, encoding="utf-8") as fh:
            for r in json.load(fh):
                if isinstance(r, dict) and r.get("id") == character:
                    return r
    except (OSError, ValueError, TypeError):
        pass
    return {}


def _number(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def reactor_room(character, path=None):
    """Whole seconds a clip must leave free for this character's intro + outro."""
    room = _number(reactor_info(character, path).get("room"))
    return REACTOR_ROOM if room is None else int(min(20, max(REACTOR_ROOM, math.ceil(room))))


def caption_bottom(character, path=None):
    """Row (of 1920) the words must end at for this character, or None to leave them where they are."""
    v = _number(reactor_info(character, path).get("caption_bottom"))
    return int(v) if v is not None and 400 <= v <= 1800 else None


def lift_ass(text, bottom, frame_h=1920):
    """The .ass text with its bottom-aligned styles' margin set so the words end at about row `bottom`.
    None when no style could be changed. Mid-frame lines (split layout, {\\an5}) are not affected."""
    m = re.search(r"^PlayResY:\s*(\d+)", text, re.M)
    res_y = int(m.group(1)) if m and int(m.group(1)) > 0 else 288
    fields, out, changed = ASS_STYLE_FIELDS, [], 0
    for line in text.splitlines():
        low = line.lower()
        if low.startswith("format:") and "marginv" in low and "fontsize" in low:
            fields = [f.strip().lower() for f in line.split(":", 1)[1].split(",")]
        elif low.startswith("style:"):
            vals = line.split(":", 1)[1].split(",")
            try:
                if len(vals) != len(fields):
                    raise ValueError
                size = float(vals[fields.index("fontsize")])
                if int(vals[fields.index("alignment")]) in (1, 2, 3):
                    margin = (frame_h - bottom) * res_y / frame_h - CAPTION_GAP * size
                    vals[fields.index("marginv")] = str(int(max(0, round(margin))))
                    line = line.split(":", 1)[0] + ":" + ",".join(vals)
                    changed += 1
            except ValueError:
                pass
        out.append(line)
    return "\n".join(out) + "\n" if changed else None


def png_size(path):
    with open(path, "rb") as fh:
        head = fh.read(24)
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("hook picture is not a PNG")
    return struct.unpack(">II", head[16:24])


def job_parts(job_dir, clip_file):
    """What OpenShorts burned a finished clip from: (video to burn onto, .ass file, hook or None).
    Names are relative to the job folder. Raises ValueError when they can't be told for sure."""
    m = re.fullmatch(r"subtitled_(\d+)_(?:hooked_(\d+)_)?(.+)", clip_file)
    if not m:
        raise ValueError("this clip has no burned-in words")
    sub_ts, hook_ts, clean = m.groups()
    subs = [f for f in os.listdir(job_dir) if re.fullmatch(r"autosubs_" + sub_ts + r"_[^_/]+\.ass", f)]
    if len(subs) != 1:
        raise ValueError(f"found {len(subs)} caption files for this clip")
    base, hook = clean, None
    if hook_ts:
        stem = f".hookrecipe_hooked_{hook_ts}_{clean}"
        try:
            with open(os.path.join(job_dir, stem + ".json"), encoding="utf-8") as fh:
                r = json.load(fh)
            w, h = png_size(os.path.join(job_dir, stem + ".png"))
            hook = {"png": stem + ".png", "x": int(r["x"]), "y": int(r["y"]), "w": w, "h": h,
                    "seconds": min(60.0, max(0.5, float(r.get("duration") or 5)))}
        except OSError:
            base = f"hooked_{hook_ts}_{clean}"   # older OpenShorts: the hook is already in this video
        except (ValueError, KeyError, TypeError):
            raise ValueError("the hook title's position couldn't be read")
    if not os.path.isfile(os.path.join(job_dir, base)):
        raise ValueError("the clip without words is no longer on disk")
    return base, subs[0], hook


def hook_row(hook, bottom):
    """Row for the hook title: where OpenShorts put it, or just above the moved words if it would
    reach into them (below the words is the reactor's box, so it never goes there)."""
    top = bottom - CAPTION_BAND - 20
    return max(0, top - hook["h"]) if hook["y"] + hook["h"] > top else hook["y"]


def run_in_job(job_id, cmd, timeout=900):
    """Run a command inside the backend container, in the job's own folder."""
    p = subprocess.run([DOCKER, "exec", "-w", f"/app/output/{job_id}", BACKEND] + cmd,
                       capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(((p.stderr or "").strip().splitlines() or [f"code {p.returncode}"])[-1][:200])


def raise_captions(job_id, clip_file, bottom, n):
    """Burn a finished clip again with its words ending at row `bottom`. Returns the new file's path
    (inside the job folder; the caller deletes it with drop_raised). Raises when it can't be done."""
    job_dir = os.path.join(REPO, "output", job_id)
    base, sub, hook = job_parts(job_dir, clip_file)
    with open(os.path.join(job_dir, sub), encoding="utf-8", errors="replace") as fh:
        lifted = lift_ass(fh.read(), bottom)
    if not lifted:
        raise ValueError("the caption file has no bottom style to move")
    ass_name, out_name = f"_raised_{n}.ass", f"_raised_{n}.mp4"
    with open(os.path.join(job_dir, ass_name), "w", encoding="utf-8") as fh:
        fh.write(lifted)
    graph = f"[0:v]ass=filename={ass_name}:fontsdir={CONTAINER_FONTS}[v]"
    inputs = ["-i", "./" + base]
    if hook:   # same order as OpenShorts: hook title first, the words burned over it
        graph = (f"[0:v][1:v]overlay={hook['x']}:{hook_row(hook, bottom)}:"
                 f"enable='between(t,0,{hook['seconds']:.2f})',"
                 f"ass=filename={ass_name}:fontsdir={CONTAINER_FONTS}[v]")
        inputs += ["-i", "./" + hook["png"]]
    run_in_job(job_id, ["ffmpeg", "-y", "-loglevel", "error"] + inputs +
               ["-filter_complex", graph, "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "17", "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", out_name])
    out = os.path.join(job_dir, out_name)
    if not os.path.isfile(out) or os.path.getsize(out) < 100000:
        raise RuntimeError("the re-burned clip came out empty")
    return out


def drop_raised(job_id, n):
    for name in (f"_raised_{n}.ass", f"_raised_{n}.mp4"):
        try:
            os.remove(os.path.join(REPO, "output", job_id, name))
        except OSError:
            pass


def reactor_tool():
    for n in REACTOR_TOOLS:
        if os.path.exists(os.path.join(REACTOR_DIR, n)):
            return os.path.join(REACTOR_DIR, n)
    return None


_REACTOR_PY = []


def reactor_python():
    """A Python that has Pillow (this script runs with -I, so user packages aren't visible here)."""
    if not _REACTOR_PY:
        found = None
        for py in (sys.executable, "/usr/bin/python3", "/opt/homebrew/bin/python3", "/usr/local/bin/python3"):
            try:
                if py and subprocess.run([py, "-c", "import PIL"], capture_output=True, timeout=30).returncode == 0:
                    found = py
                    break
            except (OSError, subprocess.TimeoutExpired):
                continue
        _REACTOR_PY.append(found)
    return _REACTOR_PY[0]


def reactor_render(character, src, dest, label):
    """Write `dest` = `src` with the reactor intro + corner box. Returns True when it worked."""
    tool = reactor_tool()
    py = reactor_python() if tool else None
    if not py:
        log(f"REACTOR: failed, the reactor tool isn't set up on this Mac: {label}")
        return False
    tmp = os.path.join(os.path.dirname(dest), "." + os.path.basename(dest)[:-4] + ".reactor.mp4")
    cues = tmp + ".cues.json"   # reactor3 notes the moments it reacted to next to its output
    env = dict(os.environ, FFMPEG=HOST_FFMPEG, STRATUS_GEMINI_MODEL=GEMINI_MODEL,
               PATH=os.path.dirname(HOST_FFMPEG) + os.pathsep + os.environ.get("PATH", ""))
    try:
        key = read_key()   # same Gemini key the clipper uses; handed to the tool in its environment only
    except OSError:
        key = ""
    if key:
        env["GEMINI_API_KEY"] = key
    err = ""
    try:
        p = subprocess.run([py, tool, "--character", character, "--clip", src, "--out", tmp],
                           capture_output=True, text=True, timeout=1200, env=env)
        ok = p.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 100000
        err = (p.stderr or "").strip().splitlines()[-1][:200] if (p.stderr or "").strip() else f"code {p.returncode}"
    except (OSError, subprocess.TimeoutExpired) as e:
        ok, err = False, str(e)[:200]
    if not ok:
        for f in (tmp, cues):
            try:
                os.remove(f)
            except OSError:
                pass
        log(f"REACTOR: failed on {os.path.basename(dest)}, kept the plain clip: {label} ({err})")
        return False
    os.replace(tmp, dest)
    if os.path.exists(cues):
        os.replace(cues, dest[:-4] + ".cues.json")
    return True


def add_reactor(folder, character, label):
    """Put the reactor on every finished clip in a Clips Ready folder (used after the hide steps)."""
    done = 0
    for f in sorted(os.listdir(folder)):
        if re.fullmatch(r"clip\d+\.mp4", f):
            path = os.path.join(folder, f)
            done += 1 if reactor_render(character, path, path, label) else 0
    log(f"REACTOR: {character} added to {done} clip(s): {label}")
    return done


def docker_run(args, timeout=4 * 3600):
    """Run the stratus-hide image (no network, capped memory). Returns the exit code."""
    cmd = [DOCKER, "run", "--rm", "--network", "none", "--memory=3g", "--cpus=3"] + args
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log("HIDE: step timed out")
        return 124
    for line in (p.stderr or "").splitlines()[-6:]:
        log("  " + line[:300])
    return p.returncode


def fetch_link_for_hiding(url, work):
    """Download a link with OpenShorts' own downloader inside the running backend,
    so the facecam can be covered before the clip job sees it."""
    rel = os.path.relpath(work, os.path.join(REPO, "output"))
    code = ("import os, sys; sys.path.insert(0, '/app'); import main; "
            "p, t = main.download_youtube_video(os.environ['HIDE_URL'], os.environ['HIDE_DIR'], use_cache=True); "
            "print('HIDE_PATH=' + p); print('HIDE_TITLE=' + (t or ''))")
    p = subprocess.run([DOCKER, "exec", "-e", "HIDE_URL=" + url, "-e", "HIDE_DIR=/app/output/" + rel,
                        BACKEND, "python", "-c", code], capture_output=True, text=True, timeout=3 * 3600)
    path = title = ""
    for line in (p.stdout or "").splitlines():
        if line.startswith("HIDE_PATH="):
            path = line[len("HIDE_PATH="):].strip()
        elif line.startswith("HIDE_TITLE="):
            title = line[len("HIDE_TITLE="):].strip()
    if p.returncode != 0 or not path:
        raise RuntimeError("download failed: " + (p.stderr or "")[-300:])
    host = os.path.join(work, os.path.relpath(path, "/app/output/" + rel))
    if not os.path.exists(host):
        raise RuntimeError("downloaded file not found on the Mac: " + host)
    return host, title


def hide_face(src, work):
    """Returns (exit_code, hidden_path). 0 = ok, 3 = streamer fills the screen."""
    name = os.path.basename(src)
    out = name + ".hidden.mp4"
    rc = docker_run(["-v", os.path.dirname(os.path.abspath(src)) + ":/in:ro", "-v", work + ":/out",
                     HIDE_IMAGE, "python", HIDE_TOOL, "face", "/in/" + name, "/out/" + out,
                     "--report", "/out/" + name + ".hide.json"])
    hidden = os.path.join(work, out)
    if rc == 0 and not os.path.exists(hidden):
        rc = 1
    return rc, hidden


def mute_on_host(path):
    tmp = path[:-4] + ".muted.mp4"
    subprocess.run([HOST_FFMPEG, "-y", "-loglevel", "error", "-i", path, "-f", "lavfi", "-i",
                    "anullsrc=r=44100:cl=stereo", "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac",
                    "-shortest", "-movflags", "+faststart", tmp], check=True, timeout=600)
    os.replace(tmp, path)


def hide_voice(folder, label):
    """Strip the voice from every clipN.mp4 in a Clips Ready folder, one at a time."""
    clips = sorted(f for f in os.listdir(folder) if re.fullmatch(r"clip\d+\.mp4", f))
    for f in clips:
        nv = f[:-4] + ".nv.mp4"
        rc = docker_run(["-v", folder + ":/w", HIDE_IMAGE, "python", HIDE_TOOL, "voice", "/w/" + f, "/w/" + nv],
                        timeout=3600)
        if rc in (0, 4) and os.path.exists(os.path.join(folder, nv)):
            os.replace(os.path.join(folder, nv), os.path.join(folder, f))
            if rc == 4:
                log(f"HIDE: muted {f[:-4]} (voice split failed): {label}")
        else:
            # never leave his voice in a clip: mute it on the Mac instead
            try:
                os.remove(os.path.join(folder, nv))
            except OSError:
                pass
            try:
                mute_on_host(os.path.join(folder, f))
                log(f"HIDE: muted {f[:-4]} (voice step failed, code {rc}): {label}")
            except Exception as e:
                os.remove(os.path.join(folder, f))
                log(f"HIDE: removed {f[:-4]} (couldn't hide the voice: {e}): {label}")
    with open(os.path.join(folder, "hidden.txt"), "w", encoding="utf-8") as fh:
        fh.write("Streamer hidden: facecam covered before clipping, voice removed from each clip "
                 "(Stratus House, %s).\n" % time.strftime("%Y-%m-%d %H:%M"))


def clip_frames(path, n=4):
    """n small JPEG frames spread across a clip (host ffmpeg), as bytes."""
    import tempfile
    out = []
    probe = subprocess.run([HOST_FFMPEG, "-hide_banner", "-i", path], capture_output=True, text=True).stderr
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", probe)
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 30.0
    with tempfile.TemporaryDirectory() as td:
        for i in range(n):
            t = dur * (i + 0.5) / n
            f = os.path.join(td, f"f{i}.jpg")
            subprocess.run([HOST_FFMPEG, "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1",
                            "-vf", "scale=512:-2", "-q:v", "5", f], timeout=60)
            if os.path.exists(f):
                with open(f, "rb") as fh:
                    out.append(fh.read())
    return out


def is_gameplay(path, key):
    """True when at least 3 of 4 frames show the game. None if the check itself failed."""
    import base64
    frames = clip_frames(path)
    if not frames:
        return None
    parts = [{"text": GAME_PROMPT.format(n=len(frames))}] + [
        {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(b).decode()}} for b in frames]
    body = json.dumps({"contents": [{"parts": parts}],
                       "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}).encode()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=body, method="POST",
                                         headers={"Content-Type": "application/json", "x-goog-api-key": key})
            with urllib.request.urlopen(req, timeout=90) as r:
                data = json.loads(r.read())
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            votes = [bool(v) for v in json.loads(re.search(r"\{.*\}", text, re.S).group(0))["gameplay"]]
            return sum(votes) >= max(1, len(votes) - 1)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 503) and attempt < 2:
                time.sleep(20)
                continue
            log(f"GAMEPLAY: check failed (HTTP {e.code})")
            return None
        except Exception as e:
            log(f"GAMEPLAY: check failed ({str(e)[:120]})")
            return None
    return None


def clip_audio(path):
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        f = os.path.join(td, "a.mp3")
        subprocess.run([HOST_FFMPEG, "-y", "-loglevel", "error", "-i", path, "-vn", "-ac", "1", "-ar", "22050",
                        "-b:a", "48k", f], timeout=120)
        if os.path.exists(f):
            with open(f, "rb") as fh:
                return fh.read()
    return None


def big_moment(path, key):
    """Gemini's verdict for one candidate: dict(conversation, big, score, kind, why), or None if the check failed."""
    import base64
    audio = clip_audio(path)
    frames = clip_frames(path)
    if not audio:
        return None
    parts = [{"text": BIG_PROMPT.format(n=len(frames))},
             {"inline_data": {"mime_type": "audio/mp3", "data": base64.b64encode(audio).decode()}}] + [
        {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(b).decode()}} for b in frames]
    body = json.dumps({"contents": [{"parts": parts}],
                       "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}).encode()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=body, method="POST",
                                         headers={"Content-Type": "application/json", "x-goog-api-key": key})
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read())
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            v = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
            return {"conversation": bool(v.get("conversation")), "big": bool(v.get("big")),
                    "score": int(v.get("score") or 0), "kind": str(v.get("kind") or "")[:30],
                    "why": str(v.get("why") or "")[:160]}
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 503) and attempt < 2:
                time.sleep(20)
                continue
            log(f"BIGMOMENT: check failed (HTTP {e.code})")
            return None
        except Exception as e:
            log(f"BIGMOMENT: check failed ({str(e)[:120]})")
            return None
    return None


def pick_big_moments(order, clips, job_id, key, name):
    """Order of candidates to keep. Conversation videos keep only big moments (best first)."""
    verdicts = {}
    for i in order:
        f = os.path.basename((clips[i].get("video_url") or "").split("?")[0])
        v = big_moment(os.path.join(REPO, "output", job_id, f), key)
        verdicts[i] = v
        if v is None:
            log(f"BIGMOMENT: candidate {i} -> check failed")
        else:
            log(f"BIGMOMENT: candidate {i} -> {'conversation' if v['conversation'] else 'not conversation'}, "
                f"{'BIG' if v['big'] else 'small'} {v['score']}/10 {v['kind']}: {v['why']}")
    checked = [v for v in verdicts.values() if v]
    if not checked:
        raise RuntimeError(f"BIGMOMENT: check failed for every clip: {name}")
    if sum(1 for v in checked if v["conversation"]) * 2 < len(checked):
        return order   # not a conversation video: normal picks
    big = [i for i in order if verdicts[i] and verdicts[i]["big"] and verdicts[i]["score"] >= BIG_MIN_SCORE]
    if not big:
        raise RuntimeError(f"BIGMOMENT: no big moment found: {name}")
    big.sort(key=lambda i: (-verdicts[i]["score"], order.index(i)))
    for i in big:
        clips[i]["big_moment"] = verdicts[i]
    return big


MEDIA_EXT = (".mp4", ".mov", ".m4v", ".mkv", ".webm", ".m4a", ".mp3", ".wav", ".aac", ".opus", ".ogg")


def scrub_job_media(job_id):
    """Delete the job's own clip/source copies: they still carry his voice."""
    root = os.path.join(REPO, "output", job_id)
    n = 0
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if f.lower().endswith(MEDIA_EXT):
                try:
                    os.remove(os.path.join(dirpath, f))
                    n += 1
                except OSError:
                    pass
    return n


def fmt_t(s):
    s = float(s or 0)
    return f"{int(s // 60)}:{s % 60:04.1f}"


def safe_name(name):
    base = os.path.splitext(os.path.basename(name))[0]
    return re.sub(r'[/:\\]+', "-", base).strip() or "video"


def hashtags(clip):
    text = " ".join(str(clip.get(k) or "") for k in
                    ("video_description_for_tiktok", "video_description_for_instagram"))
    tags = []
    for t in re.findall(r"#\w+", text):
        if t.lower() not in [x.lower() for x in tags]:
            tags.append(t)
    for t in ("#shorts", "#reels", "#fyp", "#viral"):
        if len(tags) >= 3:
            break
        if t not in tags:
            tags.append(t)
    return " ".join(tags[:5])


def draft_caption(clip):
    hook = (clip.get("viral_hook_text") or "").strip()
    desc = (clip.get("video_description_for_instagram")
            or clip.get("video_description_for_tiktok") or "").strip()
    desc = re.sub(r"\s*#\w+", "", desc).strip()
    cta = "Follow for more and drop a comment with your take 👇"
    parts = [p for p in (hook, desc) if p]
    return " — ".join(parts) + f"\n{cta}\n{hashtags(clip)}"


def write_md(folder, source, rows):
    lines = [f"# Clips from {source}", "",
             "Nothing has been posted. Review, then tell Claude which to post.", "",
             "| # | Source start–end | Length (s) | Hook line | Score | File |",
             "|---|---|---|---|---|---|"]
    for r in rows:
        star = " ⭐ top pick" if r["n"] == 1 else ""
        lines.append(f"| {r['n']}{star} | {fmt_t(r['start'])}–{fmt_t(r['end'])} | "
                     f"{r['len']:.1f} | {r['hook'] or '—'} | {r['score']} | {r['file']} |")
    lines += ["", "## Draft captions", ""]
    for r in rows:
        lines += [f"### Clip {r['n']}", "", r["caption"], ""]
    with open(os.path.join(folder, "clips.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def thumbnail(clip_host_path, job_id, clip_file, dest):
    """ffmpeg frame at 1 s, run inside the backend container (host has no ffmpeg)."""
    inside = f"/app/output/{job_id}/{clip_file}"
    tmp = f"/app/output/{job_id}/_thumb_{os.path.splitext(clip_file)[0]}.jpg"
    subprocess.run([DOCKER, "exec", BACKEND, "ffmpeg", "-y", "-loglevel", "error",
                    "-ss", "1", "-i", inside, "-frames:v", "1", "-q:v", "2", tmp],
                   check=True, timeout=120)
    shutil.move(os.path.join(REPO, "output", job_id, os.path.basename(tmp)), dest)


def collect(api, status, source, display=None, game_key=None, big_key=None, reactor=None):
    job_id = status["job_id"]
    clips = (status.get("result") or {}).get("clips") or []
    if not clips:
        raise RuntimeError("job finished with no clips")
    order = sorted(range(len(clips)),
                   key=lambda i: (-(float(clips[i].get("predicted_score") or -1)), i))
    name = display or source
    if not display and source.startswith(("http://", "https://")):
        import glob
        meta = glob.glob(os.path.join(REPO, "output", job_id, "*_metadata.json"))
        name = os.path.basename(meta[0])[:-len("_metadata.json")] if meta else "link " + job_id[:8]
    if game_key:
        keep = []
        for i in order:
            f = os.path.basename((clips[i].get("video_url") or "").split("?")[0])
            ok = is_gameplay(os.path.join(REPO, "output", job_id, f), game_key)
            log(f"GAMEPLAY: candidate {i} -> {'game play' if ok else ('not game play' if ok is False else 'check failed')}")
            if ok:
                keep.append(i)
            if len(keep) >= TOP_N:
                break
        if not keep:
            raise RuntimeError("GAMEPLAY: no game play moments found in this video")
        order = keep
    elif big_key:
        order = pick_big_moments(order, clips, job_id, big_key, os.path.basename(name).replace("_", " "))
    order = order[:TOP_N]
    bottom = caption_bottom(reactor) if reactor else None
    folder = os.path.join(OUT_ROOT, safe_name(name))
    if os.path.exists(folder):
        folder += time.strftime(" %Y-%m-%d %H%M")
    os.makedirs(folder)
    rows = []
    for n, i in enumerate(order, 1):
        c = clips[i]
        rel = c.get("video_url") or ""
        clip_file = os.path.basename(rel.split("?")[0])
        src = os.path.join(REPO, "output", job_id, clip_file)
        if not os.path.exists(src):
            raise RuntimeError(f"clip file missing on disk: {src}")
        dest = os.path.join(folder, f"clip{n}.mp4")
        # with a reactor the clip lands in Clips Ready already finished, never as a plain clip first
        added = False
        if reactor:
            label, base = os.path.basename(name), src
            try:
                if bottom:   # his box sits at the bottom: the words go just above it first
                    base = raise_captions(job_id, clip_file, bottom, n)
                added = reactor_render(reactor, base, dest, label)
            except Exception as e:
                log(f"REACTOR: failed to move the words above him on clip {n}, kept the plain clip: "
                    f"{label} ({str(e)[:200]})")
            finally:
                drop_raised(job_id, n)
        if added:
            log(f"REACTOR: {reactor} added to clip {n}: {os.path.basename(name)}")
        else:
            shutil.copy2(src, dest)
        try:
            thumbnail(src, job_id, clip_file, os.path.join(folder, f"clip{n}.jpg"))
        except Exception as e:
            log(f"thumbnail {n} failed: {e}")
        s, e = float(c.get("start") or 0), float(c.get("end") or 0)
        rows.append({"n": n, "start": s, "end": e, "len": e - s,
                     "hook": (c.get("viral_hook_text") or "").replace("|", "/"),
                     "score": c.get("predicted_score", "—"),
                     "file": f"clip{n}.mp4", "caption": draft_caption(c)})
        if c.get("big_moment"):
            rows[-1]["hook"] = (rows[-1]["hook"] + " [" + c["big_moment"]["kind"] + "]").strip()
    write_md(folder, display or os.path.basename(source), rows)
    json.dump({"job_id": job_id, "source": source, "clips": [clips[i] for i in order]},
              open(os.path.join(folder, "clips.json"), "w"), indent=1, ensure_ascii=False)
    return folder


def notify(msg):
    msg = msg.replace("\\", "/").replace('"', "'")
    subprocess.run(["osascript", "-e",
                    f'display notification "{msg}" with title "OpenShorts" sound name "Glass"'])


def main():
    video = sys.argv[1]
    dry = "--dry-run" in sys.argv
    quiet = "--quiet" in sys.argv
    is_url = video.startswith(("http://", "https://"))
    key = read_key()
    if not key:
        raise RuntimeError("GEMINI_API_KEY is empty in autopilot-keys.env")
    api = Api(key)
    api.call("GET", "/health/ready")
    if dry:
        log(f"DRY RUN ok: would process {video} (API healthy, key present). Nothing uploaded.")
        notify("Dry run OK: Clip Inbox autopilot is working. Message Claude.")
        return
    body = {"acknowledged": True, "output_format": "vertical", "captions": True,
            "auto_hook": True, "clip_max_seconds": 59, "target_clips": TOP_N}
    hide = hide_flagged(video, is_url)
    game = hide_flagged(video, is_url, GAME_FILE)
    reactor = reactor_pick(video, is_url)
    if reactor:
        body["clip_max_seconds"] = 59 - reactor_room(reactor)
    if game:
        body["target_clips"] = GAME_CANDIDATES   # more candidates, keep the best 3 that show the game
    elif not hide:
        body["target_clips"] = TALK_CANDIDATES   # more candidates, keep only the big moments of conversations
        if OPENSHORTS_HINT_FIELD:
            body[OPENSHORTS_HINT_FIELD] = BIG_HINT
    display = None
    if hide:
        label = video if is_url else os.path.basename(video)
        work = os.path.join(HIDE_WORK, re.sub(r"[^A-Za-z0-9_-]+", "_", safe_name(label))[:60] + time.strftime("_%H%M%S"))
        os.makedirs(work, exist_ok=True)
        try:
            if is_url:
                src, title = fetch_link_for_hiding(video, work)
                display = title or None
            else:
                src = video
                display = os.path.basename(video)
            rc, hidden = hide_face(src, work)
        except Exception as e:
            log(f"HIDE: failed: {label} ({str(e)[:200]})")
            shutil.rmtree(work, ignore_errors=True)
            sys.exit(EXIT_RETRY)
        if rc == 3:
            log(f"HIDE: can't hide streamer (he fills the screen): {label}")
            shutil.rmtree(work, ignore_errors=True)
            sys.exit(1)   # marked processed: skipped on purpose
        if rc != 0:
            log(f"HIDE: failed: {label} (code {rc})")
            shutil.rmtree(work, ignore_errors=True)
            sys.exit(EXIT_RETRY)
        log(f"HIDE: facecam step done: {label}")
        try:
            body["upload_id"] = upload(api, hidden)
        finally:
            shutil.rmtree(work, ignore_errors=True)   # source copies still have his voice
        body["layouts"] = ["none"]   # plain crop: no camera band / split layout
        body["captions"] = False     # no burned-in subtitles of his speech: he writes his own caption
        body["auto_hook"] = False    # no hook title card either
    elif is_url:
        body["url"] = video
    else:
        body["upload_id"] = upload(api, video)
    jid = api.call("POST", "/api/process", json.dumps(body).encode(),
                   {"Content-Type": "application/json"})["job_id"]
    print(f"JOB_ID {jid}", flush=True)
    log(f"job {jid} started")
    gk = key if game else None
    if not hide:
        wait_and_collect(api, jid, video, quiet, game_key=gk, big_key=None if game else key, reactor=reactor)
        return
    try:
        folder = wait_and_collect(api, jid, video, quiet, display, game_key=gk)
        hide_voice(folder, label)
        if reactor:   # after the voice step, or his "Check this out" would be stripped too
            add_reactor(folder, reactor, label)
    finally:
        removed = scrub_job_media(jid)   # also when the job failed: partial clips have his voice
        log(f"HIDE: {removed} leftover file(s) with his voice deleted: {label}")
    log(f"HIDE: voice step done: {label}")


def upload(api, video):
    slot = api.call("POST", "/api/uploads",
                    json.dumps({"filename": os.path.basename(video)}).encode(),
                    {"Content-Type": "application/json"})
    with open(video, "rb") as fh:
        up = api.call("PUT", f"/api/uploads/{slot['upload_id']}", fh,
                      {"Content-Length": str(os.path.getsize(video)),
                       "Content-Type": "application/octet-stream"}, timeout=3600)
    log(f"uploaded {up.get('bytes')} bytes, {up.get('duration_seconds')} s")
    return slot["upload_id"]


def wait_and_collect(api, jid, video, quiet, display=None, game_key=None, big_key=None, reactor=None):
    t0, last = time.time(), None
    while True:
        st = api.call("GET", f"/api/status/{jid}")
        s = st.get("status")
        if s != last:
            log(f"status={s} after {int(time.time() - t0)} s")
            last = s
        if s in ("completed", "done"):
            break
        if s in ("failed", "error", "cancelled"):
            raise RuntimeError(f"job {s}: " + " | ".join((st.get("logs") or [])[-6:]))
        if time.time() - t0 > 4 * 3600:
            raise RuntimeError("job still running after 4 h")
        time.sleep(20)
    st["job_id"] = jid
    folder = collect(api, st, video, display, game_key, big_key, reactor)
    log(f"saved clips to {folder}")
    print(f"SAVED {folder}", flush=True)
    if not quiet:
        notify(f"Clips ready for {os.path.basename(video)}. Message Claude to review.")
    return folder


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"ERROR: {e}")
        if "--quiet" not in sys.argv:
            notify(f"Clip Inbox failed: {str(e)[:80]}. Message Claude.")
        sys.exit(1)
