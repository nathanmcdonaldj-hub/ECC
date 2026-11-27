#!/usr/bin/env python3
"""Stratus House real reactor template (v2).

Layout (Mr. McDonald's spec, 2026-11-27):
  * The reactor sits in a gold-framed box in the lower-left corner the whole time.
  * Opening: the clip's first frame, blurred, with big "CHECK THIS OUT" text while the
    reactor says it in Mr. McDonald's own voice (lip-synced, baked into <id>-intro.mp4).
  * Then the clip plays sharp and the reactor keeps moving naturally (<id>-loop.mp4).

Usage (same call shape as v1):
  python3 reactor2.py --character studio-host --clip in.mp4 --out out.mp4 [--voice ignored]
Needs Pillow and ffmpeg (no ffprobe). Set FFMPEG=/path/to/ffmpeg if it is not on PATH.
"""
import argparse, json, os, re, shutil, subprocess, sys, tempfile
from PIL import Image, ImageDraw, ImageFilter, ImageEnhance

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "assets")
FF = os.environ.get("FFMPEG") or shutil.which("ffmpeg") or os.path.expanduser("~/stratus-media/bin/ffmpeg")
W, H, FPS = 1080, 1920, 30
GOLD = (232, 190, 92)
BOX, BORDER = 380, 8
BX, BY = 28, 1040           # lower-left; above burned-in captions and app buttons
TITLE_Y = 640


def run(args):
    subprocess.run([FF, "-hide_banner", "-v", "error", "-y"] + args, check=True)


def duration(path):
    p = subprocess.run([FF, "-hide_banner", "-i", path], stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", p.stderr)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def frame_assets(tmp):
    r = BOX // 9
    s = BOX - 2 * BORDER
    m = Image.new("L", (s, s), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, s - 1, s - 1], r - BORDER, fill=255)
    m.save(os.path.join(tmp, "mask.png"))
    fr = Image.new("RGBA", (BOX + 40, BOX + 40), (0, 0, 0, 0))
    sh = Image.new("L", fr.size, 0)
    ImageDraw.Draw(sh).rounded_rectangle([20, 26, BOX + 20, BOX + 32], r, fill=170)
    fr.putalpha(sh.filter(ImageFilter.GaussianBlur(12)))
    ring = Image.new("RGBA", (BOX, BOX), (0, 0, 0, 0))
    d = ImageDraw.Draw(ring)
    d.rounded_rectangle([0, 0, BOX - 1, BOX - 1], r, fill=GOLD + (255,))
    fr.alpha_composite(ring, (20, 20))
    hole = Image.new("L", fr.size, 255)
    ImageDraw.Draw(hole).rounded_rectangle([20 + BORDER, 20 + BORDER, 20 + BOX - 1 - BORDER, 20 + BOX - 1 - BORDER],
                                           r - BORDER, fill=0)
    fr.putalpha(Image.composite(fr.getchannel("A"), Image.new("L", fr.size, 0), hole))
    fr.save(os.path.join(tmp, "frame.png"))


def box_chain(src, crop, out):
    x, y, s = crop
    inner = BOX - 2 * BORDER
    return (f"[{src}]crop={s}:{s}:{x}:{y},scale={inner}:{inner},fps={FPS},setsar=1,format=rgba[{out}v];"
            f"[{out}v][{out}m]alphamerge[{out}]")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--character", default="studio-host")
    ap.add_argument("--clip", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--voice", help="ignored: his voice is baked into the intro video")
    a = ap.parse_args()

    cfg = {c["id"]: c for c in json.load(open(os.path.join(HERE, "reactors.json")))}
    if a.character not in cfg:
        sys.exit(f"unknown reactor {a.character}")
    c = cfg[a.character]
    intro, loop = os.path.join(ASSETS, c["intro"]), os.path.join(ASSETS, c["loop"])
    title = os.path.join(ASSETS, "check-this-out.png")
    tmp = tempfile.mkdtemp(prefix="reactor-")
    try:
        frame_assets(tmp)
        mask, frame = os.path.join(tmp, "mask.png"), os.path.join(tmp, "frame.png")
        f0 = os.path.join(tmp, "f0.png")
        run(["-i", a.clip, "-frames:v", "1",
             "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}", f0])
        bg = Image.open(f0).convert("RGB").filter(ImageFilter.GaussianBlur(24))
        ImageEnhance.Brightness(bg).enhance(0.6).save(os.path.join(tmp, "bg.png"))

        dint = duration(intro)
        di = dint + 0.25
        l0 = os.path.join(tmp, "loop0.png")   # first loop frame: clean cut to it when he finishes the line (a dissolve ghosts two faces)
        run(["-i", loop, "-frames:v", "1", l0])
        ox, oy, ix, iy = BX - 20, BY - 20, BX + BORDER, BY + BORDER

        # 1) opening: blurred first frame + CHECK THIS OUT + talking reactor (his voice)
        op = os.path.join(tmp, "open.mp4")
        fc = (f"[0:v]fps={FPS},setsar=1,format=rgba[bg];" + box_chain("1:v", c["intro_crop"], "I") + ";"
              "[2:v]format=gray,split[Im][Jm];" + box_chain("5:v", c["loop_crop"], "J") + ";"
              f"[J]fade=in:st={dint:.3f}:d=0.04:alpha=1[Jf];"
              f"[bg][I]overlay={ix}:{iy}:eof_action=repeat[b0];[b0][Jf]overlay={ix}:{iy}[b1];[b1][3:v]overlay={ox}:{oy}[b2];"
              f"[4:v]format=rgba,fade=in:st=0:d=0.2:alpha=1[t];[b2][t]overlay=(W-w)/2:{TITLE_Y},format=yuv420p[v];"
              f"[1:a]atrim=start={c.get('audio_shift', 0):.3f},asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,apad[a]")
        run(["-loop", "1", "-t", f"{di:.3f}", "-i", os.path.join(tmp, "bg.png"), "-i", intro,
             "-loop", "1", "-i", mask, "-loop", "1", "-i", frame, "-loop", "1", "-i", title, "-loop", "1", "-i", l0,
             "-filter_complex", fc, "-map", "[v]", "-map", "[a]", "-t", f"{di:.3f}", "-r", str(FPS),
             "-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-c:a", "aac", "-b:a", "192k", op])

        # 2) seamless idle loop (forward + reverse)
        pp = os.path.join(tmp, "pingpong.mp4")
        run(["-i", loop, "-filter_complex", "[0:v]split[a][b];[b]reverse[r];[a][r]concat=n=2:v=1:a=0[v]",
             "-map", "[v]", "-an", "-c:v", "libx264", "-crf", "16", "-preset", "veryfast", pp])

        # 3) clip with live reactor, joined after the opening
        fc = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS},setsar=1[c];"
              f"[2:v]format=gray[Lm];" + box_chain("1:v", c["loop_crop"], "L") + ";"
              f"[c][L]overlay={ix}:{iy}:shortest=1[m1];[m1][3:v]overlay={ox}:{oy}:shortest=1,format=yuv420p[m];"
              f"[0:a]aresample=48000,aformat=channel_layouts=stereo[ma];"
              f"[4:v]fps={FPS},setsar=1,format=yuv420p[iv];[4:a]aresample=48000,aformat=channel_layouts=stereo[ia];"
              f"[iv][ia][m][ma]concat=n=2:v=1:a=1[v][a]")
        run(["-i", a.clip, "-stream_loop", "-1", "-i", pp, "-loop", "1", "-i", mask, "-loop", "1", "-i", frame,
             "-i", op, "-filter_complex", fc, "-map", "[v]", "-map", "[a]", "-r", str(FPS),
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "19", "-preset", "veryfast",
             "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", a.out])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(a.out)


if __name__ == "__main__":
    main()
