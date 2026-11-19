# Stratus House Media dashboard

A local dashboard for the clip autopilot: a "studio floor" of little workers,
one per step, from adding a video to posting, stats, KDP book sales, UGC
videos and a Social Media Manager you can talk to.

## What it needs

- macOS with Apple's Python 3.9+ (`/usr/bin/python3`). No other packages.
- The OpenShorts autopilot folders: `~/Movies/Clip Inbox`, `~/Movies/Clips Ready`.
- Optional: `ffmpeg` in `~/stratus-media/bin/` for background music.

## Install

```bash
bash apps/stratus-media/install.sh
```

It copies the app to `~/stratus-media`, starts it at login with a low-priority
LaunchAgent (`com.stratushouse.media`) and adds **Stratus House Media** to
`~/Applications`. The dashboard runs at <http://127.0.0.1:5190> and only
answers this Mac.

## Where things live

| What | Where |
| --- | --- |
| Queue, posts, sales, chat | `~/Library/Application Support/StratusMedia/state.json` |
| Keys (never shown) | `~/Library/Application Support/StratusMedia/secrets.json` (mode 600) |
| Settings overrides | `~/Library/Application Support/StratusMedia/config.json` |
| Music tracks | `~/Movies/Stratus Music` |
| Finished UGC videos | `~/Movies/UGC Videos` |
| Log | `~/Library/Logs/StratusMedia.log` |

Set `"dry_run_posting": true` in `config.json` to test without posting.

## Remove

```bash
launchctl bootout gui/$(id -u)/com.stratushouse.media
rm ~/Library/LaunchAgents/com.stratushouse.media.plist
```
