---
name: makethevideoandpost
description: "Use when sent audio with makethevideoandpost → IG Reel."
version: 1.0.0
author: Hermes Agent (for @inatmania)
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [instagram, reels, ffmpeg, stt, video, publishing, makethevideoandpost, inatmania]
---

# makethevideoandpost

Turns a raw audio file into a published **Instagram Reel** on @inatmania: minimalist
pure black & white 1080p video, dead-center bold captions, no b-rolls, one-shot pipeline
(STT → analytics → render → IG Audio API music → upload (`video_url` ingest, resumable as
fallback) → publish → permalink).

## When to Use

**Immediately** when the user sends (or references) an audio file — `.m4a`, `.mp3`, `.wav`, `.ogg` —
together with any of these phrases:

- `makethevideoandpost`
- `/makethevideoandpost`
- `make the video and post`

The platform exposes the downloaded file path in the incoming message; use that path.
If only a voice-note reference exists, obtain the file path first, then run.

## HARD RULES (user-locked — never weaken)

1. **Model lock:** active provider `commandcode`, model `deepseek/deepseek-v4.1-flash`.
   **NEVER** switch the provider or model automatically, for any reason.
2. **STT-only keys:** `GROQ_API_KEY` / `OPENROUTER_API_KEY` are used ONLY for Speech-to-Text
   (Whisper transcription) inside the script. **NEVER** make LLM/chat calls with them, never spam
   them, and ALWAYS ask the user for explicit permission first before using either key for
   anything other than STT.
3. **API budget per run:** at most ONE Groq STT call, and ONE OpenRouter STT call (fallback only,
   on Groq 429/failure). Never loop or retry beyond that.
4. **No test posts:** never publish anything except a real user-triggered audio run.
5. **No b-rolls / images / stock footage / transitions / effects.** Pure black canvas + centered
   white captions only.
6. **Never convert `.m4a` to `.mp3`** — the original audio goes straight to STT and is muxed
   directly into the final MP4.
7. Temporary audio/video/subtitle files are deleted after publishing (the script does this;
   pass `--keep-temp` only for debugging).

## How to run

```bash
python3 ~/.hermes/scripts/makethevideoandpost.py "/path/to/audio.m4a"
```

Use a generous terminal timeout (600–900s): a 90s clip renders in well under a minute on this box,
but long audio + container processing + publishing adds time.

Parse the final output lines:

- `PERMALINK: https://www.instagram.com/reel/…` → the live post URL (or `(dry-run)`)
- `RESULT_JSON: {…}` → full decision record (aspect ratio, font, music, caption, stt_source)

**Do not run the script twice for the same audio** — if a previous run already returned a
`PERMALINK`, that post is live and must not be duplicated.

## Reply format (Telegram — required)

Reply with the live permalink, then a 2-line summary, e.g.:

```
Published ✅ https://www.instagram.com/reel/XXXX/
• 9:16 vertical (1080x1920) · Montserrat ExtraBold 72 · share_to_feed=true · STT: groq
• Music: "Dark Ambient…" (audio_id 123) | no music · Caption: "<first line>"
```

If the run failed, reply with the failure reason and the last `[mtvap]` log line.

## What the script decides autonomously

- **Aspect ratio:** `9:16` (1080x1920, audio ≤90s) or `16:9` (1920x1080 when audio >90s, so the
  ≤90s Reel constraint is never violated by trimming).
- **Feed distribution:** `share_to_feed=false` if a feed post went live <12h ago, else `true`.
- **Typography:** `Montserrat ExtraBold` @72 (default/impact) or `Poppins ExtraBold` @68 for
  melancholic tone; sizes: 72 for 1080x1920, 68 for 1920x1080.
- **Music:** Instagram Audio API `GET /ig_audio` mood queries — instrumental only
  ("dark ambient instrumental", "melancholic piano instrumental", "slow minimal ambient",
  "calm dark cello"), attached via `audio_configuration` at `audio_volume: 20`.
  Fallbacks: next candidate (≤2 tries) → container without `audio_configuration` (never fails;
  licensing subcode 2207065 handled).
- **Caption:** hook sentence from the transcript + top account hashtags + niche tags.
- **Cover:** `thumb_offset=2000`; **upload:** resumable via `rupload.facebook.com`.

Flags: `--dry-run` (render only, nothing published) · `--caption "…"` override ·
`--location "City"` (attaches location_id when found) · `--keep-temp` · `--check-auth`
(verifies IG creds via `GET /<IG_USER_ID>?fields=id,username`) · `--upload-mode auto|resumable|url`
(`auto` default: one resumable attempt, then `video_url`; `url` skips resumable).

`RESULT_JSON.upload_mode` records which path actually published the reel.

## Environment requirements

- `~/.hermes/.env` (chmod 600) must contain: `IG_USER_ID`, `IG_ACCESS_TOKEN`, `GROQ_API_KEY`,
  `OPENROUTER_API_KEY`.
- `ffmpeg` + `ffprobe` (with libass/libx264/loudnorm) — reinstall hook: `~/.agent37/hooks/post-image-update.sh`.
- Fonts in `~/.local/share/fonts/` (Montserrat + Poppins + Inter) + `fc-cache -f`.
- Local STT safety net (auto-installs once if both cloud APIs fail): `faster-whisper` small/int8 CPU.

## Known pitfalls (hard-won — read before debugging an upload failure)

1. **The resumable upload endpoint (`rupload.facebook.com`) is unreliable from this box —
   use `video_url` ingest instead.** Symptom: `POST …/ig-api-upload/v27.0/<container>` returns
   `{"type":"ProcessingFailedError","retriable":false,"message":"Request processing failed"}`
   (http 400/500), sometimes with `Video Transcoding Error: both HD and SD progressive failed to
   transcode`. The *same file* uploads and publishes fine when Instagram pulls it itself:
   create the container with `media_type=REELS&video_url=<public mp4>` and poll
   `GET /<container>?fields=status_code` until `FINISHED`, then `media_publish`.
   The script now does this automatically: `--upload-mode auto` (default) tries resumable once,
   then falls back to serving the render via `agent37 expose` and passing `video_url`.
   Use `--upload-mode url` to skip the doomed resumable attempt entirely (saves ~90s).
2. **That error message is a liar.** It is identical for genuinely bad files and for perfectly
   good ones, and it is not retriable. Nine retries across three containers produced nine
   identical failures; the identical bytes then published through `video_url` on the first try.
   Do not burn time bisecting size/bitrate/duration — go straight to the URL path, then
   investigate the render only if the URL path also fails.
3. **Audio sample rate still matters.** `loudnorm` upsamples to 96 kHz and `-c:a aac` keeps it, so
   the renderer forces `-ar 48000` on the encoder and probes the output, aborting if it is not
   44100/48000. Keep that guard: 96 kHz audio is rejected by every ingest path. Note this was
   *not* the cause of the 2026-11 incident (the file already failed at 48 kHz too).
4. **`aresample=48000` inside `-af` breaks mono** (`Cannot select channel layout…`) — put
   `-ar 48000` on the encoder side instead.
5. **`agent37 expose` reserves 8080 and 8443.** `http.server` on 8080 silently loses to the
   platform's listener and you get a confusing 404. `LocalHoster` picks a free port from 8090 up.
6. **Diagnose read-only.** `GET /<container_id>?fields=status_code` is free and unpublishable:
   `IN_PROGRESS` forever = bytes never accepted, `FINISHED` = file is good to publish,
   `ERROR` = real transcode failure. Also check `agent37 list-exposed` after a run — a crashed
   URL-ingest run can leave a public port behind.

## Troubleshooting

- `ProcessingFailedError` on the rupload POST → switch to `--upload-mode url` (pitfall 1). This is
  the expected failure mode here, not a symptom of a bad render.
- `video_url` container stuck `IN_PROGRESS` >5 min → Instagram could not fetch the URL; check
  `agent37 list-exposed`, that the port was reachable, and that the file is a plain `.mp4`.
- Groq 429 / failure → OpenRouter fallback runs automatically; both failing → local faster-whisper.
- Per-post insights require the `instagram_manage_insights` permission on the IG token. Without it, the
  script skips them fast (circuit breaker) and proceeds with public metrics (likes/comments/timestamps,
  hashtags) — that is expected behavior, not an error. Add the permission to the Meta app/token to enable them.
- The IG Audio API may return a fixed 'eligible' list regardless of the mood query. The script keeps only
  tracks that clearly match instrumental/calm/dark/melancholic keywords; if none match it publishes
  **without** background music (by design — never off-brief audio).
- `container status: ERROR/EXPIRED` → rerun with `--keep-temp` and read the last `[mtvap]` lines.
- `IG_ACCESS_TOKEN` expired → refresh via Meta Graph Explorer and re-upsert into `~/.hermes/.env`.

## Backup & restore (public GitHub repo)

- Repo (**public**): `makethevideoandpost-skill` —
  `https://github.com/AdarshJ173/makethevideoandpost-skill` (public mirror maintained from this machine; restore instructions below).
- Restore on a fresh container:

```bash
git clone https://github.com/AdarshJ173/makethevideoandpost-skill.git /tmp/mtvap-restore
mkdir -p ~/.hermes/scripts
cp /tmp/mtvap-restore/makethevideoandpost.py ~/.hermes/scripts/ && chmod +x ~/.hermes/scripts/makethevideoandpost.py
mkdir -p ~/.hermes/skills/video/makethevideoandpost
cp /tmp/mtvap-restore/SKILL.md ~/.hermes/skills/video/makethevideoandpost/SKILL.md
# then re-add IG_USER_ID / IG_ACCESS_TOKEN / GROQ_API_KEY / OPENROUTER_API_KEY to ~/.hermes/.env (chmod 600)
```
