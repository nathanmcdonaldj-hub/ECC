#!/usr/bin/env python3
"""Hide a streamer in Stratus House clips. Runs inside the OpenShorts backend container
(it already has ffmpeg, opencv, mediapipe and torch; the stratus-hide image adds demucs).

  hide_streamer.py face  SOURCE.mp4 OUT.mp4 [--report r.json]
      Finds a static facecam (webcam inset), covers it with a heavy blur and crops off the edges
      where stream overlays sit (chat, sub alerts, taskbar). Audio is kept,
      so OpenShorts can still transcribe and pick moments. Exit code 0 = covered or no face,
      3 = the streamer fills the screen (can't be hidden by covering), anything else = error.

  hide_streamer.py voice CLIP.mp4 OUT.mp4
      Removes the voice (demucs two-stem split) and keeps game/background sound. If the split
      fails, the clip is muted instead, so his voice never slips through.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

FFMPEG = os.environ.get("FFMPEG", shutil.which("ffmpeg") or "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", shutil.which("ffprobe") or "ffprobe")


def log(msg):
    sys.stderr.write("[HideStreamer] {}\n".format(msg))


def run(cmd, timeout=3600):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError("{} failed: {}".format(os.path.basename(cmd[0]), p.stderr[-600:]))
    return p.stdout


def probe(path):
    out = json.loads(run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height:format=duration", "-of", "json", path]))
    st = out["streams"][0]
    return int(st["width"]), int(st["height"]), float(out["format"]["duration"])


# ---------------------------------------------------------------- facecam

def face_boxes(path, duration, samples=48):
    """Normalized face boxes (x, y, w, h) from frames spread across the video."""
    import cv2
    import mediapipe as mp
    cap = cv2.VideoCapture(path)
    boxes, seen = [], 0
    det = mp.solutions.face_detection.FaceDetection(model_selection=1, min_detection_confidence=0.55)
    try:
        for i in range(samples):
            t = duration * (i + 0.5) / samples
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, frame = cap.read()
            if not ok:
                continue
            seen += 1
            res = det.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            for d in (res.detections or []):
                b = d.location_data.relative_bounding_box
                boxes.append((max(0.0, b.xmin), max(0.0, b.ymin), b.width, b.height))
    finally:
        cap.release()
        det.close()
    return boxes, seen


def median(vals):
    v = sorted(vals)
    return v[len(v) // 2]


def facecam_rect(boxes, seen):
    """Pick the face that keeps showing up in the same place: that's the facecam."""
    if not boxes or seen == 0:
        return None, "noface"
    # cluster by center (faces within 12% of the frame of each other)
    clusters = []
    for b in boxes:
        cx, cy = b[0] + b[2] / 2, b[1] + b[3] / 2
        for c in clusters:
            if abs(c["cx"] - cx) < 0.12 and abs(c["cy"] - cy) < 0.12:
                c["items"].append(b)
                break
        else:
            clusters.append({"cx": cx, "cy": cy, "items": [b]})
    best = max(clusters, key=lambda c: len(c["items"]))
    if len(best["items"]) < max(3, seen * 0.25):
        return None, "noface"   # faces only now and then (e.g. in the game): nothing static to cover
    x, y, w, h = (median([b[k] for b in best["items"]]) for k in range(4))
    if w * h > 0.10:
        return None, "fullframe"   # IRL / just chatting: he IS the picture
    # a webcam shows head + shoulders: grow the face box into the inset
    cx, cy = x + w / 2, y + h / 2
    W, H = w * 3.8, h * 4.2   # generous: webcam frames are wider than the head (no room strip left over)
    rx, ry = cx - W / 2, cy - H * 0.42
    # snap to the nearest edges when close (insets usually sit in a corner)
    if rx < 0.12:
        rx = 0.0
    if ry < 0.12:
        ry = 0.0
    if rx + W > 0.88:
        W = 1.0 - rx
    if ry + H > 0.88:
        H = 1.0 - ry
    rx, ry = max(0.0, rx), max(0.0, ry)
    W, H = min(W, 1.0 - rx), min(H, 1.0 - ry)
    return (rx, ry, W, H), "facecam"


# Stream overlays live at the edges: chat (left or right), sub alerts (top), taskbar (bottom).
# Keep only the middle of the picture. Override with HIDE_CROP="x,y,w,h" (fractions).
CROP = tuple(float(v) for v in os.environ.get("HIDE_CROP", "0.14,0.07,0.72,0.84").split(","))


def crop_filter(vw, vh):
    x, y, w, h = CROP
    W, H = int(vw * w) // 2 * 2, int(vh * h) // 2 * 2
    return "crop={}:{}:{}:{}".format(W, H, int(vw * x) // 2 * 2, int(vh * y) // 2 * 2)


def encode(src, dst, filter_complex, vmap):
    run(["nice", "-n", "15", FFMPEG, "-y", "-loglevel", "error", "-i", src, "-filter_complex", filter_complex,
         "-map", vmap, "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-pix_fmt", "yuv420p", "-threads", "3", "-c:a", "copy", "-movflags", "+faststart", dst], timeout=7200)


def cover(src, dst, rect, vw, vh):
    x, y, w, h = rect
    X, Y = int(x * vw) // 2 * 2, int(y * vh) // 2 * 2
    Wp, Hp = max(16, int(w * vw) // 2 * 2), max(16, int(h * vh) // 2 * 2)
    Wp, Hp = min(Wp, vw - X), min(Hp, vh - Y)
    r = max(8, min(Wp, Hp) // 5)
    fc = ("[0:v]split[a][b];[b]crop={W}:{H}:{X}:{Y},scale=iw/8:-2,boxblur={r2}:4,scale={W}:{H},boxblur={r}:3[m];"
          "[a][m]overlay={X}:{Y},{crop}[v]").format(W=Wp, H=Hp, X=X, Y=Y, r=r, r2=max(2, r // 8),
                                                     crop=crop_filter(vw, vh))
    encode(src, dst, fc, "[v]")
    return {"x": X, "y": Y, "w": Wp, "h": Hp}


def stage_face(src, dst, report=None):
    vw, vh, dur = probe(src)
    boxes, seen = face_boxes(src, dur)
    rect, kind = facecam_rect(boxes, seen)
    result = {"kind": kind, "frames": seen, "faces": len(boxes)}
    if kind == "facecam":
        result["rect"] = cover(src, dst, rect, vw, vh)
        log("facecam covered at {}".format(result["rect"]))
    elif kind == "noface":
        encode(src, dst, "[0:v]{}[v]".format(crop_filter(vw, vh)), "[v]")
        log("no steady face found; edges with chat/alerts cropped off")
    if report:
        with open(report, "w") as fh:
            json.dump(result, fh)
    return 3 if kind == "fullframe" else 0


# ---------------------------------------------------------------- voice

def split_no_vocals(wav_in, wav_out):
    """htdemucs via its Python API (no torchaudio I/O: torchaudio 2.11 needs torchcodec to save).
    Writes everything except the vocals stem as 16-bit stereo WAV."""
    import numpy as np
    import torch
    from scipy.io import wavfile
    from demucs.apply import apply_model
    from demucs.pretrained import get_model
    torch.set_num_threads(int(os.environ.get("HIDE_THREADS", "3")))
    sr, data = wavfile.read(wav_in)
    if data.ndim == 1:
        data = np.stack([data, data], axis=1)
    x = torch.from_numpy(data.T.astype(np.float32) / 32768.0)
    model = get_model("htdemucs")
    model.eval()
    if sr != model.samplerate:
        raise RuntimeError("expected {} Hz audio, got {}".format(model.samplerate, sr))
    ref = x.mean(0)
    mean, std = ref.mean(), ref.std() + 1e-8
    with torch.no_grad():
        out = apply_model(model, ((x - mean) / std)[None], device="cpu", split=True, overlap=0.25,
                          segment=7, num_workers=0, progress=False)[0]
    out = out * std + mean
    voc = model.sources.index("vocals")
    bed = sum(out[i] for i in range(len(model.sources)) if i != voc)
    wavfile.write(wav_out, sr, (bed.clamp(-1, 1).numpy().T * 32767).astype(np.int16))


def stage_voice(src, dst):
    tmp = tempfile.mkdtemp(prefix="hide_")
    try:
        wav = os.path.join(tmp, "a.wav")
        run([FFMPEG, "-y", "-loglevel", "error", "-i", src, "-vn", "-ac", "2", "-ar", "44100", wav])
        bed = None
        try:
            cand = os.path.join(tmp, "no_vocals.wav")
            split_no_vocals(wav, cand)
            if os.path.exists(cand):
                bed = cand
        except Exception as exc:  # noqa: BLE001
            log("voice split failed, muting instead: {}".format(str(exc)[-200:]))
        if bed:
            run([FFMPEG, "-y", "-loglevel", "error", "-i", src, "-i", bed, "-map", "0:v", "-map", "1:a",
                 "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", dst])
            log("voice removed")
            return 0
        run([FFMPEG, "-y", "-loglevel", "error", "-i", src, "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
             "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-shortest", "-movflags", "+faststart", dst])
        log("muted (no voice split available)")
        return 4
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv):
    if len(argv) < 4 or argv[1] not in ("face", "voice"):
        sys.stderr.write(__doc__)
        return 2
    try:
        if argv[1] == "face":
            report = argv[argv.index("--report") + 1] if "--report" in argv else None
            return stage_face(argv[2], argv[3], report)
        return stage_voice(argv[2], argv[3])
    except Exception as exc:  # noqa: BLE001
        log("error: {}".format(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
