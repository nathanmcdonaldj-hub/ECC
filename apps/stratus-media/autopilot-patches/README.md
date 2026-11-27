# Autopilot patches: "Hide the streamer" (face + voice)

These are copies of the Mac's clipping scripts in `~/openshorts/autopilot/` after
the Hide the streamer feature was wired in on 2026-11-20. The Mac keeps the
previous versions as `*.bak-2026-11-20` next to the scripts.

OpenShorts itself was **not** changed. It already has a per-job request option
that turns off the camera/split layouts.

## process_video.py

A source is flagged when the dashboard's Inbox Clerk switch writes it to
`~/Movies/Clip Inbox/hide.json` (`{"links": [url], "files": [name]}`). Links match
by exact URL and files match by basename.

**Pre-step (before OpenShorts sees the video)**

- **Link:** the video is downloaded with OpenShorts' own downloader
  (`main.download_youtube_video`, same cache and free route as a normal job) via
  `docker exec openshorts-backend`, into `~/openshorts/output/_hide/<name>/`.
- **File:** the inbox file is used as is (mounted read-only).
- **Face step:** `docker run --rm --network none --memory=3g --cpus=3 stratus-hide
  python /opt/stratus/hide_streamer.py face ...` covers the facecam. The covered
  `.hidden.mp4` is uploaded to OpenShorts instead of the original. The job is sent
  with `layouts: ["none"]` (AUTO_LAYOUT=0), so no camera band or split layout is
  used. The SCREENCAST layout that holds the camera-inset split is off by default
  in this deployment (SCREENCAST_LAYOUT is unset).
- **Exit 3** (streamer fills the screen): the video is skipped and marked
  processed. The log says `HIDE: can't hide streamer (he fills the screen): <name>`.
- **Any other failure:** the script exits 75. The log says `HIDE: failed: <name>`,
  and night_run.sh leaves the item queued for retry. A flagged video is never
  clipped unhidden.
- The work folder (original and covered source, both still with his voice) is
  deleted right after the upload.

**Post-step (after the clips land in `~/Movies/Clips Ready/<video>/`)**

- **Voice step:** for each `clipN.mp4`, one at a time,
  `hide_streamer.py voice /w/clipN.mp4 /w/clipN.nv.mp4` runs in the same capped
  container.
  - Exit 0 or 4: the clip is replaced. Exit 4 means it was muted, and the log says
    `HIDE: muted clipN (voice split failed): <name>`.
  - Anything else: the clip is muted on the Mac with ffmpeg. If even that fails,
    the clip is deleted. A clip with his voice is never left behind.
- **Cleanup:** every media file in the job's `~/openshorts/output/<job_id>/` is
  deleted (source and clean/caption-less copies that still have his voice). This
  also happens when the job fails partway.
- `hidden.txt` is written in the clip folder.
- Burned-in captions are kept as they are.

## night_run.sh

One added line: exit code **75** from process_video.py means "hiding failed".
The item is not added to `processed.txt`, so it is retried on the next run.
Everything else is unchanged.

## Reactor (2026-11-27)

The dashboard's Inbox Clerk lets Mr. McDonald pick a reactor for a video (None, Studio Host, Felt
Puppet, Gold Mask). The pick is saved in `~/Movies/Clip Inbox/reactor.json`
(`{"links": {url: character}, "files": {name: character}}`).

- For a picked video, clips are capped at 56 s so the ~2 s intro keeps them under 60 s.
- Each finished clip (captions already burned in) is run through
  `~/stratus-media/tools/reactor/reactor.py` before it lands in Clips Ready: the reactor says
  "Check this out" in his own voice, then sits in a corner box. Music and the end tag are still
  added later by the dashboard.
- The voice is ONE reusable recording at
  `~/Library/Application Support/StratusMedia/reactor/voice.<m4a|wav|mp3>`. Without it the picker is
  greyed out and no reactor is added.
- Needs a Python with Pillow and the dashboard's ffmpeg (`~/stratus-media/bin/ffmpeg`).
- Fail-open: if the step can't run, the plain clip is kept and the log line starts with
  `REACTOR: failed`, which the dashboard shows in plain words.
- With "Hide the streamer" on, the reactor is added after the voice-removal step.
