# Trend briefs feed

The nightly Trend Scout (8 PM Phoenix) writes `latest.json` here, plus a dated copy `YYYY-MM-DD.json`.
The Stratus House Media dashboard on the Mac reads `latest.json` from this branch and shows it in the
Trend Scout sheet. Shape: `{report, date, generated_at, brief_url, concerns[], ideas[{rank, pick, niche,
title, length, why, hook, caption, sound, post_time, build_plan[]}]}`, at most 3 ideas, exactly one `pick`.
