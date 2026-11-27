#!/usr/bin/env python3
"""Stratus House reactor template.

Adds a reactor character to a 9:16 clip:
  1. Intro: the reactor, big, says "Check this out" (mouth moves with the voice file).
  2. The reactor shrinks into a corner box and the clip plays.

Usage:
  python3 reactor.py --character studio-host --clip in.mp4 --voice check.m4a --out out.mp4
Characters: studio-host, felt-puppet, gold-mask. Needs ffmpeg + Pillow.
"""
import argparse, math, os, subprocess, sys, tempfile
from array import array
from PIL import Image, ImageDraw, ImageFilter, ImageEnhance

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "assets")
W, H, FPS = 1080, 1920, 30
GOLD, NAVY = (232, 190, 92), (0, 12, 64)
BIG, BIG_CY = 820, 1060          # intro box size / centre y
SMALL, MARGIN, TOP = 360, 40, 110  # corner box size / right margin / top


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, **kw)


def boxed(img, size):
    """Square character picture with rounded corners, gold border and shadow."""
    pad = max(6, size // 40)
    r = size // 9
    out = Image.new("RGBA", (size + 40, size + 40), (0, 0, 0, 0))
    sh = Image.new("L", out.size, 0)
    ImageDraw.Draw(sh).rounded_rectangle([20, 26, size + 20, size + 32], r, fill=150)
    out.putalpha(sh.filter(ImageFilter.GaussianBlur(12)))
    frame = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(frame).rounded_rectangle([0, 0, size - 1, size - 1], r, fill=GOLD + (255,))
    inner = img.convert("RGB").resize((size - 2 * pad, size - 2 * pad), Image.LANCZOS)
    m = Image.new("L", inner.size, 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, inner.size[0] - 1, inner.size[1] - 1], r - pad, fill=255)
    frame.paste(inner, (pad, pad), m)
    out.alpha_composite(frame, (20, 20))
    return out


def voice_levels(path, n_frames=None):
    raw = run(["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
              stdout=subprocess.PIPE).stdout
    a = array("h"); a.frombytes(raw[: len(raw) // 2 * 2])
    hop = 16000 // FPS
    lv = []
    for i in range(0, len(a), hop):
        seg = a[i:i + hop]
        lv.append(math.sqrt(sum(x * x for x in seg) / max(1, len(seg))))
    peak = max(lv) or 1
    return [v / peak for v in lv], len(a) / 16000


def mouth_track(levels):
    """Open when loud; force a short close every few frames so the flap reads."""
    st, run_open = [], 0
    for v in levels:
        o = v > 0.22
        if o:
            run_open += 1
            if run_open > 4 and run_open % 5 == 0:
                o = False
        else:
            run_open = 0
        st.append(o)
    return st


def ease(t):
    return t * t * (3 - 2 * t)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--character", required=True, choices=["studio-host", "felt-puppet", "gold-mask"])
    ap.add_argument("--clip", required=True)
    ap.add_argument("--voice", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    idle = Image.open(os.path.join(ASSETS, f"{a.character}-idle.png"))
    talk = Image.open(os.path.join(ASSETS, f"{a.character}-talk.png"))
    title = Image.open(os.path.join(ASSETS, "check-this-out.png")).convert("RGBA")
    big_idle, big_talk = boxed(idle, BIG), boxed(talk, BIG)
    small_idle = boxed(idle, SMALL)

    tmp = tempfile.mkdtemp(prefix="reactor-")
    # background for intro: first frame of the clip, blurred and darkened
    run(["ffmpeg", "-v", "error", "-y", "-i", a.clip, "-frames:v", "1",
         "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}", os.path.join(tmp, "f0.png")])
    first = Image.open(os.path.join(tmp, "f0.png")).convert("RGB")
    bg = ImageEnhance.Brightness(first.filter(ImageFilter.GaussianBlur(18))).enhance(0.45).convert("RGBA")

    levels, vdur = voice_levels(a.voice)
    mouth = mouth_track(levels)
    shrink = int(0.4 * FPS)
    talk_frames = len(levels) + int(0.15 * FPS)
    total = talk_frames + shrink

    big_xy = ((W - big_idle.width) // 2, BIG_CY - big_idle.height // 2)
    end_xy = (W - MARGIN - SMALL - 20, TOP - 20)

    intro = os.path.join(tmp, "intro.mp4")
    enc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
                            "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-i", a.voice,
                            "-af", "apad", "-t", f"{total / FPS:.3f}",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", "-preset", "veryfast",
                            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", intro],
                           stdin=subprocess.PIPE)
    for i in range(total):
        fr = bg.copy()
        if i < talk_frames:
            is_talk = i < len(mouth) and mouth[i]
            box = big_talk if is_talk else big_idle
            pop = ease(min(1, i / 6))           # quick pop-in
            s = 0.85 + 0.15 * pop + (0.012 if is_talk else 0)
            bw = int(box.width * s)
            b = box.resize((bw, bw), Image.LANCZOS)
            fr.alpha_composite(b, (W // 2 - bw // 2, BIG_CY - bw // 2))
            ta = title.copy(); ta.putalpha(ta.getchannel("A").point(lambda v: int(v * pop)))
            fr.alpha_composite(ta, ((W - ta.width) // 2, BIG_CY - BIG // 2 - 210))
        else:
            t = ease((i - talk_frames + 1) / shrink)
            # fade background toward the real first frame while the box flies to the corner
            fr = Image.blend(bg.convert("RGB"), first, t).convert("RGBA")
            size = int(big_idle.width + (small_idle.width - big_idle.width) * t)
            b = big_idle.resize((size, size), Image.LANCZOS)
            x = int(big_xy[0] + (end_xy[0] - big_xy[0]) * t)
            y = int(big_xy[1] + (end_xy[1] - big_xy[1]) * t)
            fr.alpha_composite(b, (x, y))
        enc.stdin.write(fr.convert("RGB").tobytes())
    enc.stdin.close(); enc.wait()
    if enc.returncode:
        sys.exit("intro encode failed")

    small_png = os.path.join(tmp, "small.png"); small_idle.save(small_png)
    # main: clip with the corner box (gentle breathing bob), then join intro + main
    fc = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS},setsar=1[c];"
          f"[c][1:v]overlay=x={end_xy[0]}:y='{end_xy[1]}+4*sin(2*PI*t/3)':shortest=1[m];"
          f"[0:a]aresample=48000,aformat=channel_layouts=stereo[ma];"
          f"[2:v]fps={FPS},setsar=1[iv];[2:a]aresample=48000,aformat=channel_layouts=stereo[ia];"
          f"[iv][ia][m][ma]concat=n=2:v=1:a=1[v][a]")
    run(["ffmpeg", "-v", "error", "-y", "-i", a.clip, "-loop", "1", "-i", small_png, "-i", intro,
         "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "19", "-preset", "veryfast",
         "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", a.out])
    print(a.out)


if __name__ == "__main__":
    main()
