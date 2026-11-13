---
name: makethevideoandpost
description: "Use when sent audio with makethevideoandpost → IG Reel."
version: 1.2.0
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
(STT → analytics → render → IG Audio API music → upload → publish → permalink).

Two upload paths exist. **`video_url` ingest is the one that works on this box**; the
resumable `rupload.facebook.com` POST is kept only as a fallback attempt. See PITFALLS.
Raw Graph API call sequence (container → poll → publish → permalink): `references/ig-reels-graph-api.md`.

**Maintenance rule:** when a run teaches you something, rewrite the section that misled you.
Do not append "UPDATE: actually…" notes or a second copy of a rule — the next session reads the
whole file top-down and will follow whichever version it hits first.

## When to Use

**Immediately** when the user sends (or references) an audio file — `.m4a`, `.mp3`, `.wav`, `.ogg` —
together with any of these phrases:

- `makethevideoandpost`
- `/makethevideoandpost`
- `make the video and post`

The platform exposes the downloaded file path in the incoming message; use that path.
If only a voice-note reference exists, obtain the file path first, then run.

If audio arrives with **no trigger phrase at all**, the intent is ambiguous and publishing is
public and irreversible — ask once (`clarify`, first option "Yes — render and publish"), then run.
Do not silently publish a memo the user meant as a draft.

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
8. **Music: dark/melancholic instrumentals only, at `audio_volume: 70`, and never the same track
   two reels in a row.** `AUDIO_VOLUME_PCT = 70` and the rotation history in
   `~/.hermes/state/mtvap_music_history.json` implement this. Do not lower the volume back to a
   "tasteful" 20 and do not disable rotation — both were explicit user instructions.
9. **Re-publishing to "fix" a post is only allowed after the user deletes the old one.** The API
   cannot delete IG media, so a re-upload creates a duplicate on the grid; say so before running.

## How to run

```bash
python3 ~/.hermes/scripts/makethevideoandpost.py "/path/to/audio.m4a" --upload-mode url
```

Defaults matter here:

- `--upload-mode auto` (default) = one resumable attempt, then automatic `video_url` fallback.
- `--upload-mode url` = skip the resumable attempt entirely. **Use this by default** — it removes
  ~90 s of guaranteed failure per run and cannot leave a half-uploaded container behind.
- `--upload-mode resumable` = debug only.

Run it in the background (`terminal(background=true, notify=true)`) and pipe to a real log file,
not `| tail`. `tail` buffers everything until exit and leaves you blind for five minutes:

```bash
python3 ~/.hermes/scripts/makethevideoandpost.py "<audio>" --upload-mode url > ~/.hermes/cache/scratch/run.log 2>&1
```

Parse the final output lines:

- `PERMALINK: https://www.instagram.com/reel/…` → the live post URL (or `(dry-run)`)
- `RESULT_JSON: {…}` → full decision record (aspect, font, music, caption, `stt_source`, `upload_mode`)

**Do not run the script twice for the same audio** — if a previous run already returned a
`PERMALINK`, that post is live and must not be duplicated. The one exception is a run that died
after render and before publish (see RESCUE PATH).

## Reply format (Telegram — required)

```
Published ✅ https://www.instagram.com/reel/XXXX/
• 9:16 vertical (1080x1920) · Montserrat ExtraBold 72 · share_to_feed=false · STT: groq
• Music: "Dark Ambient…" (audio_id 123) | no music · Caption: "<first line>"
```

If the run failed, reply with the failure reason and the last `[mtvap]` log line.

## What the script decides autonomously

- **Aspect ratio:** `9:16` (1080x1920, audio ≤90s) or `16:9` (1920x1080 when audio >90s, so the
  ≤90s Reel constraint is never violated by trimming).
- **Feed distribution:** `share_to_feed=false` if a feed post went live <12h ago, else `true`.
- **Typography:** `Montserrat ExtraBold` @72 (default/impact) or `Poppins ExtraBold` @68 for
  melancholic tone; sizes: 72 for 1080x1920, 68 for 1920x1080.
- **Music:** Instagram Audio API `GET /ig_audio` mood queries — **dark / melancholic instrumentals
  only** ("dark ambient instrumental", "melancholic piano instrumental", "slow minimal ambient",
  "calm dark cello"; every mood map leads with these). Attached via `audio_configuration` at
  **`audio_volume: 70`** (`AUDIO_VOLUME_PCT`) — user-locked preference, never silently lower it.
  **Rotate the track every run:** used `audio_id`s are recorded in
  `~/.hermes/state/mtvap_music_history.json` and already-used tracks are pushed behind fresh ones,
  so two consecutive reels never share background music. Fallbacks: next candidate (≤2 tries) →
  container without `audio_configuration` (never fails; licensing subcode 2207065 handled).
- **Music history seeding:** after a run that published with music, `record_music_used()` appends
  the id; to force a different track on the next run, seed that file with the id just used. The `video_url` path honours music the same way — if a
  manual rescue publishes without `audio_configuration`, report "no music" honestly.
- **Caption:** hook sentence from the transcript + top account hashtags + niche tags.
- **Cover:** `thumb_offset=2000` (half the duration if the clip is <2.6s).

## PITFALLS (hard-won — read this before debugging anything)

1. **`rupload.facebook.com` is broken from this box; `video_url` ingest is not.**
   Symptom: `POST …/ig-api-upload/v27.0/<container>` → http 400/500 with
   `{"type":"ProcessingFailedError","retriable":false,"message":"Request processing failed"}`,
   sometimes upgraded to `Video Transcoding Error: both HD and SD progressive failed to transcode`.
   The identical file reaches `FINISHED` and publishes in one try when Instagram fetches it:
   create the container with `media_type=REELS&video_url=<public mp4>`, poll
   `GET /<container>?fields=status_code` until `FINISHED`, then `media_publish`.
   `LocalHoster` in the script does the serving + `agent37 expose` + teardown.
2. **That error message lies.** It is byte-identical for good and bad renders and is marked
   non-retriable, yet it fires intermittently depending on file duration — which tempts you into
   a size/bitrate/duration bisection. That bisection burned ~20 probes and an hour and proved
   nothing: 1080p failed at 30–86s, 360x640 passed at 60s, an 18 MB 5s clip passed. **Rule: if the
   POST fails on a real render, switch to `video_url` immediately and only inspect the render if
   the URL path also fails.**
3. **Never re-render to "fix" an upload rejection** before testing the URL path. A 96 kHz audio
   track was a real defect (see 4) but fixing it did not make the resumable path work — the file
   was fine all along.
4. **96 kHz audio must never ship.** `loudnorm` upsamples to 96 kHz and `-c:a aac` keeps it. The
   renderer forces `-ar 48000` on the encoder and probes the output, aborting unless it is
   44100/48000. Keep the guard and the flag.
5. **`aresample=48000` inside `-af` breaks mono** — `Cannot select channel layout for the link
   between filters`. Put `-ar 48000` on the encoder side, not in the filter chain.
6. **Ports 8080 and 8443 are reserved by the platform.** `http.server` on 8080 loses to the
   platform listener and answers 404 for your own file, which looks like a missing render.
   `LocalHoster` picks a free port from 8090 upward; also `--bind 0.0.0.0`.
7. **Diagnose read-only, never by publishing.** `GET /<container_id>?fields=status_code` costs
   nothing: `IN_PROGRESS` forever = bytes never accepted; `FINISHED` = good to publish;
   `ERROR` = real transcode failure. `fields=owner` is invalid on containers (error 100) — use
   `status_code,status`.
8. **Publish verification is a second call.** `media_publish` returns only a media id; fetch
   `GET /<media_id>?fields=permalink,media_type,timestamp` to get the reel URL you report.
9. **Instagram's Graph API cannot delete a published reel.** `DELETE /<media_id>` returns
   `(#10) Insufficient permissions`, and `DELETE /<ig-user-id>/media` is unsupported — there is no
   media-delete endpoint for IG Graph. When the user asks to remove a post, say so plainly and tell
   them to delete it in the Instagram app; only then re-upload. Never re-publish "as a fix" while
   the old post is still live — that leaves a duplicate on the grid.
10. **Clean up after a URL-ingest run:** `agent37 unexpose <port>`, kill the http server, delete
    the render, then confirm with `agent37 list-exposed`. A crashed run leaves a public port
    serving the file — an accidental public link to the user's content.
11. **`status_code` polling needs ~60–90 s** before `FINISHED`; poll every 10 s, cap at ~10 min,
    and do not interpret the first `IN_PROGRESS` as failure.

## RESCUE PATH — run died after render, before publish

When `--keep-temp` was used (or the run failed mid-way and the temp dir still exists):

1. Find the render: `~/.hermes/cache/scratch/makethevideoandpost-*/reel.mp4` (+ `subs.ass`).
2. Publish it with the helper (no second STT call — the caption is rebuilt from `subs.ass`):

```bash
python3 ~/.hermes/skills/video/makethevideoandpost/scripts/rescue_publish.py \
  ~/.hermes/cache/scratch/makethevideoandpost-XXXX --share-to-feed
```

   It creates the container with `video_url`, polls to `FINISHED`, publishes, prints `PERMALINK`.
   `--dry-run` creates and polls the container without publishing; `--music-mood` attaches audio.
3. Doing it by hand: `LocalHoster(tmpdir)` → `create_container_smart(..., video_url=…)` →
   `wait_finished` → `publish` → `fetch_permalink` (import the pipeline script as a module).
4. Tear down the hoster, then delete the temp dir. Report `upload_mode: video_url` and "no music"
   if `audio_configuration` was not attached.

This exact path published a real 86 s reel on the first attempt after nine resumable failures —
it is the reliable fallback, not a hack.

## Troubleshooting

- `ProcessingFailedError` on the rupload POST → `--upload-mode url` (pitfall 1). Expected here.
- `video_url` container stuck `IN_PROGRESS` >5 min → Instagram could not fetch the file: confirm the
  public URL returns `Content-type: video/mp4` from outside, that the port is in `agent37 list-exposed`,
  and that the path has no spaces.
- Groq 429 / failure → OpenRouter fallback runs automatically; both failing → local faster-whisper.
- Per-post insights require `instagram_manage_insights`. Without it the script skips them fast and
  proceeds with public metrics — expected behaviour, not an error.
- The IG Audio API may return a fixed 'eligible' list regardless of mood. The script keeps only
  clearly instrumental/calm/dark/melancholic tracks; if none match it publishes **without** music
  (by design — never off-brief audio).
- `container status: ERROR/EXPIRED` → rerun with `--keep-temp` and read the `[mtvap]` lines, then
  use the RESCUE PATH.
- `IG_ACCESS_TOKEN` expired → refresh via Meta Graph Explorer and re-upsert into `~/.hermes/.env`.

## Environment requirements

- `~/.hermes/.env` (chmod 600): `IG_USER_ID`, `IG_ACCESS_TOKEN`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`.
- `ffmpeg` + `ffprobe` (libass/libx264/loudnorm) — reinstall hook: `~/.agent37/hooks/post-image-update.sh`.
- Fonts in `~/.local/share/fonts/` (Montserrat + Poppins + Inter) + `fc-cache -f`.
- `agent37` CLI on PATH (needed for `expose`/`unexpose` on the URL-ingest path).
- Local STT safety net (auto-installs once if both cloud APIs fail): `faster-whisper` small/int8 CPU.

## Backup & restore (public GitHub repo)

- Repo (**public**): `https://github.com/AdarshJ173/makethevideoandpost-skill`
  (public mirror maintained from this machine; restore instructions below).
- Restore on a fresh container:

```bash
git clone https://github.com/AdarshJ173/makethevideoandpost-skill.git /tmp/mtvap-restore
mkdir -p ~/.hermes/scripts
cp /tmp/mtvap-restore/makethevideoandpost.py ~/.hermes/scripts/ && chmod +x ~/.hermes/scripts/makethevideoandpost.py
mkdir -p ~/.hermes/skills/video/makethevideoandpost
cp /tmp/mtvap-restore/SKILL.md ~/.hermes/skills/video/makethevideoandpost/SKILL.md
# then re-add IG_USER_ID / IG_ACCESS_TOKEN / GROQ_API_KEY / OPENROUTER_API_KEY to ~/.hermes/.env (chmod 600)
```

## Changelog

- **1.2.0** — music preferences locked in: dark/melancholic query order for every mood,
  `audio_volume: 70` (was 20), and per-run track rotation via
  `~/.hermes/state/mtvap_music_history.json` (`load_music_history` / `record_music_used`).
  Documented that the Graph API cannot delete media.
- **1.1.0** — `video_url` ingest path (+ `LocalHoster`, `--upload-mode auto|resumable|url`);
  `-ar 48000` render guard; PITFALLS + RESCUE PATH sections from the failed-upload incident;
  verified end-to-end by publishing https://www.instagram.com/reel/DfryWhVE8PA/.
- **1.0.0** — initial one-shot pipeline.
