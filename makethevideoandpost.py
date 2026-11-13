#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
makethevideoandpost.py — Minimalist audio -> Instagram Reel pipeline (1080p).

End-to-end:
  1. Speech-to-Text (STT ONLY): Groq `whisper-large-v3-turbo`  ->  OpenRouter
     `openai/whisper-large-v3-turbo` fallback (only on 429/failure)  ->  local
     faster-whisper `small` int8 CPU safety net (only if both cloud APIs fail).
  2. Pre-publish analytics: last 15 media items (+ insights) of the IG account,
     used to drive creative decisions (aspect ratio, feed distribution,
     typography, hashtags, music mood).
  3. Minimalist pure black & white render — NO b-rolls / images / effects:
     pure black 1080p canvas @30fps, dead-center (Alignment 5) bold sans-serif
     captions, 3-5 words per cue, white text + crisp black outline.
  4. Instagram Audio API (GET /ig_audio): mood-matched *instrumental* background
     music (dark/calm/melancholic only), attached via audio_configuration at
     volume 20, with a clean fallback to no-music so publishing never fails.
  5. Caption + resumable upload (rupload.facebook.com) -> poll -> publish ->
     fetch live permalink -> delete all temporary audio/video/subtitle files.

STRICT GUARDRAILS (never weaken):
  * GROQ_API_KEY / OPENROUTER_API_KEY are used in this script ONLY for STT.
    Never make LLM/chat calls with them; ask the user for explicit permission
    before using either key for anything other than transcription.
  * This script never switches the Hermes provider/model.
  * The original audio file is passed directly to STT and muxed directly into
    the final MP4 — .m4a is NEVER converted to .mp3.

Usage:
  python3 ~/.hermes/scripts/makethevideoandpost.py <audio.(m4a|mp3|wav|ogg)> [--dry-run]
  python3 ~/.hermes/scripts/makethevideoandpost.py --check-auth   # verify IG creds

Output ends with:
  PERMALINK: <live Instagram URL>          (or "(dry-run)")
  RESULT_JSON: {...machine-readable result...}
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

try:  # requests is preferred when available; the script also ships a stdlib path
    import requests  # type: ignore
except Exception:  # pragma: no cover
    requests = None

__version__ = "1.0.0"

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
ENV_FILE = HERMES_HOME / ".env"
GRAPH = "https://graph.facebook.com/v27.0"
RUPLOAD = "https://rupload.facebook.com/ig-api-upload/v27.0"
GROQ_STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
OPENROUTER_STT_URL = "https://openrouter.ai/api/v1/audio/transcriptions"
FFMPEG_LOGLEVEL = os.environ.get("MTVAP_FFMPEG_LOGLEVEL", "warning")

# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------


def log(msg: str) -> None:
    print(f"[mtvap] {msg}", flush=True)


def die(msg: str, code: int = 1) -> None:
    print(f"[mtvap][ERROR] {msg}", file=sys.stderr, flush=True)
    raise SystemExit(code)


def _short(content, n: int = 300) -> str:
    if isinstance(content, (bytes, bytearray)):
        return content[:n].decode("utf-8", "replace")
    return str(content)[:n]


def load_dotenv(path: Path = ENV_FILE) -> None:
    """Load KEY=VALUE pairs from ~/.hermes/.env into the environment."""
    if not path.exists():
        return
    for raw in path.read_text(errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _hours_ago(ts, now: datetime | None = None):
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None
    return ((now or _utcnow()) - dt).total_seconds() / 3600.0


def _audio_mime(path: Path) -> str:
    return {
        ".m4a": "audio/mp4",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".ogg": "audio/ogg",
        ".opus": "audio/opus",
        ".flac": "audio/flac",
        ".webm": "audio/webm",
    }.get(path.suffix.lower(), "application/octet-stream")


# ---------------------------------------------------------------------------
# HTTP layer (requests when available, urllib fallback)
# ---------------------------------------------------------------------------


def _urllib_request(method, url, headers=None, params=None, data=None, files=None, body=None, timeout=60):
    hdrs = dict(headers or {})
    if params:
        sep = "&" if "?" in url else "?"
        url = url + sep + urllib.parse.urlencode(params, doseq=True)
    payload = None
    if files:
        boundary = "----mtvap" + uuid.uuid4().hex
        buf = io.BytesIO()

        def _w(x):
            buf.write(x if isinstance(x, (bytes, bytearray)) else str(x).encode("utf-8"))

        for name, value in (data or {}).items():
            values = value if isinstance(value, (list, tuple)) else [value]
            for v in values:
                _w(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n')
                _w(str(v))
                _w("\r\n")
        for name, spec in files.items():
            if isinstance(spec, (tuple, list)):
                fname, fobj = spec[0], spec[1]
                ctype = spec[2] if len(spec) >= 3 else "application/octet-stream"
            else:
                fname, fobj, ctype = name, spec, "application/octet-stream"
            _w(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{fname}"\r\nContent-Type: {ctype}\r\n\r\n')
            if hasattr(fobj, "seek"):
                fobj.seek(0)
            _w(fobj.read())
            _w("\r\n")
        _w(f"--{boundary}--\r\n")
        payload = buf.getvalue()
        hdrs["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif body is not None:
        payload = body if isinstance(body, (bytes, bytearray)) else str(body).encode("utf-8")
    elif data is not None:
        payload = urllib.parse.urlencode(data, doseq=True).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    req = urllib.request.Request(url, data=payload, headers=hdrs, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return getattr(resp, "status", resp.getcode()), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:  # network errors etc.
        return 0, f"{type(e).__name__}: {e}".encode()


def http_request(method, url, headers=None, params=None, data=None, files=None, body=None, timeout=60):
    """Return (status_code, content_bytes). status 0 = network-level failure."""
    if requests is not None:
        try:
            kwargs: dict = {"headers": headers, "timeout": timeout}
            if params is not None:
                kwargs["params"] = params
            if files is not None:
                kwargs["files"] = files
                if data is not None:
                    kwargs["data"] = data
            elif body is not None:
                kwargs["data"] = body
            elif data is not None:
                kwargs["data"] = data
            r = requests.request(method, url, **kwargs)
            return r.status_code, r.content
        except Exception as e:
            return 0, f"{type(e).__name__}: {e}".encode()
    return _urllib_request(method, url, headers=headers, params=params, data=data, files=files, body=body, timeout=timeout)


class GraphError(Exception):
    def __init__(self, status, payload):
        super().__init__(f"graph http {status}: {_short(json.dumps(payload), 400)}")
        self.status = status
        self.payload = payload

    @property
    def subcode(self):
        err = (self.payload or {}).get("error") or {}
        return err.get("subcode")


def _parse_graph(status, content):
    try:
        payload = json.loads(content.decode("utf-8", "replace"))
    except Exception:
        payload = {"error": {"message": _short(content, 400)}}
    if isinstance(payload, dict) and "error" in payload:
        raise GraphError(status, payload)
    return payload


def graph_get(path, params=None, token=None, timeout=60):
    token = token or os.environ.get("IG_ACCESS_TOKEN", "")
    params = dict(params or {})
    params["access_token"] = token
    status, content = http_request("GET", GRAPH + path, params=params, timeout=timeout)
    return _parse_graph(status, content)


def graph_post(path, params=None, token=None, timeout=120):
    token = token or os.environ.get("IG_ACCESS_TOKEN", "")
    params = dict(params or {})
    params["access_token"] = token
    status, content = http_request("POST", GRAPH + path, data=params, timeout=timeout)
    return _parse_graph(status, content)


# ---------------------------------------------------------------------------
# Step A — STT (Groq -> OpenRouter -> local faster-whisper).  *STT ONLY*
# ---------------------------------------------------------------------------


def probe_duration(path: Path) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    try:
        duration = float(proc.stdout.strip())
    except Exception:
        die(f"could not probe audio duration for {path} (ffprobe said: {proc.stdout.strip()!r} {proc.stderr.strip()!r})")
    return duration


PUNCT_ONLY_RE = re.compile(r"^[\W_]+$", re.UNICODE)


def merge_punct(words):
    """Attach punctuation-only tokens to the previous word."""
    out = []
    for w in words:
        token = str(w.get("word", "")).strip()
        if not token:
            continue
        try:
            start, end = float(w.get("start", 0.0)), float(w.get("end", 0.0))
        except Exception:
            continue
        if out and PUNCT_ONLY_RE.match(token):
            out[-1]["word"] += token
            out[-1]["end"] = max(out[-1]["end"], end)
        else:
            out.append({"word": token, "start": start, "end": end})
    return out


def parse_stt_payload(payload):
    """Return (words, text). Words are token dicts with start/end seconds."""
    if not isinstance(payload, dict):
        return [], ""
    words = []
    for w in payload.get("words") or []:
        if not isinstance(w, dict):
            continue
        token = str(w.get("word", "")).strip()
        if not token:
            continue
        try:
            words.append({"word": token, "start": float(w["start"]), "end": float(w["end"])})
        except Exception:
            continue
    text = str(payload.get("text") or "").strip()
    if not words:
        # fall back to segments: split each segment's text into tokens with
        # evenly interpolated timing so captions can still be synced.
        for seg in payload.get("segments") or []:
            try:
                st, en = float(seg.get("start", 0.0)), float(seg.get("end", 0.0))
            except Exception:
                continue
            toks = str(seg.get("text", "")).split()
            if not toks or en <= st:
                continue
            step = (en - st) / len(toks)
            for i, tok in enumerate(toks):
                words.append({"word": tok, "start": st + i * step, "end": st + (i + 1) * step})
    return merge_punct(words), text


def _attempt_cloud_stt(url, model, audio_path, api_key, service):
    """One single attempt against one service — no retries, no spamming."""
    data = {"model": model, "response_format": "verbose_json", "timestamp_granularities[]": "word"}
    headers = {"Authorization": f"Bearer {api_key}"}
    with open(audio_path, "rb") as fh:
        files = {"file": (os.path.basename(audio_path), fh, _audio_mime(Path(audio_path)))}
        status, content = http_request("POST", url, headers=headers, data=data, files=files, timeout=600)
    if status != 200:
        return None, f"{service} http {status}: {_short(content)}"
    try:
        payload = json.loads(content.decode("utf-8", "replace"))
    except Exception as e:
        return None, f"{service} bad JSON: {e}"
    words, text = parse_stt_payload(payload)
    if not words:
        return None, f"{service} returned no word timestamps"
    return {"words": words, "text": text, "source": service}, None


def stt_local(audio_path):
    """Offline safety net — local faster-whisper (small / int8 / CPU)."""
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError:
        log("faster-whisper not installed — attempting a one-time install (only because both cloud APIs failed)…")
        attempts = []
        if shutil.which("uv"):
            attempts.append(["uv", "pip", "install", "--system", "faster-whisper"])
        attempts.append([sys.executable, "-m", "pip", "install", "--user", "--quiet", "faster-whisper"])
        attempts.append([sys.executable, "-m", "pip", "install", "--user", "--quiet", "--break-system-packages", "faster-whisper"])
        for cmd in attempts:
            subprocess.run(cmd, check=False, capture_output=True)
            try:
                from faster_whisper import WhisperModel  # type: ignore
                break
            except ImportError:
                continue
        else:
            die("faster-whisper unavailable — offline STT safety net not possible. Install manually: pip install faster-whisper")
    model = WhisperModel("small", device="cpu", compute_type="int8")
    segments, _info = model.transcribe(str(audio_path), word_timestamps=True)
    words, text_parts = [], []
    for seg in segments:
        text_parts.append((seg.text or "").strip())
        for w in getattr(seg, "words", None) or []:
            token = str(getattr(w, "word", "")).strip()
            if token:
                words.append({"word": token, "start": float(w.start), "end": float(w.end)})
    words = merge_punct(words)
    if not words:
        die("local faster-whisper produced no word timestamps")
    return {"words": words, "text": " ".join(t for t in text_parts if t), "source": "faster-whisper"}


def transcribe(audio_path):
    groq_key = os.environ.get("GROQ_API_KEY", "").strip()
    or_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if groq_key:
        log("STT: trying Groq (whisper-large-v3-turbo)…")
        result, err = _attempt_cloud_stt(GROQ_STT_URL, "whisper-large-v3-turbo", audio_path, groq_key, "groq")
        if result:
            log("STT: Groq OK")
            return result
        log(f"STT: Groq unavailable -> {err}")
    else:
        log("STT: GROQ_API_KEY not set — skipping Groq")
    if or_key:
        log("STT: trying OpenRouter fallback (openai/whisper-large-v3-turbo)…")
        result, err = _attempt_cloud_stt(OPENROUTER_STT_URL, "openai/whisper-large-v3-turbo", audio_path, or_key, "openrouter")
        if result:
            log("STT: OpenRouter OK")
            return result
        log(f"STT: OpenRouter unavailable -> {err}")
    else:
        log("STT: OPENROUTER_API_KEY not set — skipping OpenRouter")
    log("STT: both cloud APIs unavailable — falling back to local faster-whisper (small/int8 CPU), no more API calls")
    return stt_local(audio_path)


# ---------------------------------------------------------------------------
# Step B — pre-publish account analytics & creative decisions
# ---------------------------------------------------------------------------

MELANCHOLIC_KEYS = ["sad", "alone", "lonely", "dark", "night", "lost", "death", "cold", "miss",
                    "cry", "broken", "fear", "hurt", "empty", "silence", "pain", "gone", "tears",
                    "drowning", "falling", "buried", "ghost"]
ENERGETIC_KEYS = ["never", "always", "win", "best", "money", "success", "grind", "hustle", "run",
                  "fight", "power", "strong", "stop", "change", "million", "free", "top", "rise", "!"]

MOOD_QUERIES = {
    "melancholic": ["melancholic piano instrumental", "dark ambient instrumental",
                    "slow minimal ambient", "calm dark cello"],
    "energetic": ["calm ambient instrumental", "slow minimal ambient",
                  "dark ambient instrumental", "melancholic piano instrumental"],
    "neutral": ["slow minimal ambient", "dark ambient instrumental",
                "calm dark cello", "melancholic piano instrumental"],
}


def fetch_account_context(user_id, token):
    ctx = {"media": [], "insights": {}, "hashtags": Counter(),
           "median_likes": None, "latest_feed_hours": None, "latest_reel_hours": None}
    fields = "id,caption,media_type,media_product_type,timestamp,like_count,comments_count,permalink"
    try:
        data = graph_get(f"/{user_id}/media", {"fields": fields, "limit": 15}, token=token)
    except GraphError as e:
        log(f"analytics: media list unavailable ({_short(e)}) — continuing with defaults")
        return ctx
    items = data.get("data") or []
    ctx["media"] = items
    now = _utcnow()
    feed_hours, reel_hours = [], []
    for it in items:
        caption = it.get("caption") or ""
        for tag in re.findall(r"#([A-Za-z0-9_]+)", caption):
            ctx["hashtags"]["#" + tag.lower()] += 1
        hours = _hours_ago(it.get("timestamp"), now)
        if hours is not None:
            product_type = it.get("media_product_type")
            if product_type == "REELS":
                reel_hours.append(hours)
            if product_type == "FEED":
                feed_hours.append(hours)
    if feed_hours:
        ctx["latest_feed_hours"] = min(feed_hours)
    if reel_hours:
        ctx["latest_reel_hours"] = min(reel_hours)
    likes = sorted(int(it.get("like_count") or 0) for it in items)
    if likes:
        ctx["median_likes"] = likes[len(likes) // 2]
    # Best-effort insights on the 5 most recent media (bounded calls, circuit
    # breaker: tokens without instagram_manage_insights fail fast).
    metric_attempts = ["reach,likes,comments,shares,saved,total_views", "reach,shares,saved", "reach"]
    consecutive_failures = 0
    for it in items[:5]:
        got = None
        for metrics in metric_attempts:
            try:
                ins = graph_get(f"/{it['id']}/insights", {"metric": metrics}, token=token)
            except GraphError:
                continue
            got = {}
            for m in ins.get("data") or []:
                values = m.get("values") or [{}]
                got[m.get("name") or "metric"] = values[0].get("value")
            break
        if got is not None:
            ctx["insights"][it["id"]] = got
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            if consecutive_failures >= 2:
                log("per-post insights unavailable for this token (e.g. missing instagram_manage_insights) — continuing with public metrics")
                break
    return ctx


def decide(duration, text, ctx):
    """Autonomous creative decisions driven by transcript + account performance."""
    lowered = (text or "").lower()
    mel = sum(lowered.count(k) for k in MELANCHOLIC_KEYS)
    en = sum(lowered.count(k) for k in ENERGETIC_KEYS)
    mood = "melancholic" if mel > en else ("energetic" if en > mel else "neutral")

    if duration <= 90.0:
        aspect, (width, height), size = "9:16", (1080, 1920), 72
        reason = f"9:16 vertical — audio is {duration:.1f}s (fits the <=90s Reel window)"
    else:
        aspect, (width, height), size = "16:9", (1920, 1080), 68
        reason = f"16:9 horizontal — audio is {duration:.1f}s (>90s)"

    # Typography: soften (Poppins) for melancholic tone, impact font otherwise.
    font = "Poppins ExtraBold" if mood == "melancholic" else "Montserrat ExtraBold"

    # Feed distribution: avoid flooding the grid if a feed post went live <12h ago.
    gap = ctx.get("latest_feed_hours")
    share_to_feed = not (gap is not None and gap < 12)

    return {"aspect": aspect, "res": (width, height), "size": size, "font": font, "mood": mood,
            "share_to_feed": share_to_feed, "music_queries": list(MOOD_QUERIES.get(mood, MOOD_QUERIES["neutral"])),
            "reason": reason}


def build_caption(text, ctx, mood):
    clean = re.sub(r"\s+", " ", text or "").strip()
    clean = re.sub(r"#\w+|@\w+", "", clean).strip()
    sentences = re.split(r"(?<=[.!?])\s+", clean)
    hook = (sentences[0].strip() if sentences and sentences[0].strip() else "")
    if len(hook) > 140:
        hook = hook[:140].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    if not hook:
        hook = "New drop."
    if hook[-1] not in ".!?…":
        hook += "."
    tags = [tag for tag, _count in ctx["hashtags"].most_common(5)]
    if mood == "melancholic":
        base = ["#reels", "#deepwords", "#mood", "#night"]
    elif mood == "energetic":
        base = ["#reels", "#explore", "#viral", "#motivation"]
    else:
        base = ["#reels", "#explore", "#mindset"]
    for tag in base:
        if tag not in tags:
            tags.append(tag)
    tags = tags[:8]
    return hook + "\n\n" + " ".join(tags)


# ---------------------------------------------------------------------------
# Step C — minimalist pure black & white render (no b-rolls)
# ---------------------------------------------------------------------------


def group_words(words, max_words=5, min_words=3):
    cues, cur = [], []
    total = len(words)
    for i, w in enumerate(words):
        cur.append(w)
        gap = (words[i + 1]["start"] - w["end"]) if i + 1 < total else None
        ends_punct = bool(re.search(r"[.,!?;:…]$", w["word"]))
        long_pause = gap is not None and gap > 0.6
        if len(cur) >= max_words or (len(cur) >= min_words and (ends_punct or long_pause)):
            cues.append(cur)
            cur = []
    if cur:
        if cues and len(cur) < min_words and len(cues[-1]) < max_words + 1:
            cues[-1].extend(cur)
        else:
            cues.append(cur)
    return cues


def fmt_ts(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds - 3600 * hours - 60 * minutes
    return f"{hours}:{minutes:02d}:{secs:05.2f}"


def build_ass(cues, res, font, size, out_path: Path) -> None:
    width, height = res
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {width}\n"
        f"PlayResY: {height}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "YCbCr Matrix: TV.709\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,"
        "0,0,0,0,100,100,0,0,1,3,0,5,60,60,60,1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = [header]
    for cue in cues:
        start = float(cue[0]["start"])
        end = float(cue[-1]["end"])
        if end - start < 0.20:
            end = start + 0.20
        text = " ".join(str(w["word"]) for w in cue)
        text = text.replace("{", "(").replace("}", ")").replace("\n", " ")
        lines.append(f"Dialogue: 0,{fmt_ts(start)},{fmt_ts(end)},Default,,0,0,0,,{text}\n")
    Path(out_path).write_text("".join(lines), encoding="utf-8")


def render_video(audio_path, ass_path, out_mp4, res, duration) -> None:
    width, height = res
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", FFMPEG_LOGLEVEL,
        "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:r=30",
        "-i", str(audio_path),
        "-map", "0:v:0", "-map", "1:a:0",
        "-vf", f"subtitles={Path(ass_path).name}",
        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        "-t", f"{max(0.05, float(duration)):.3f}",
        "-shortest",
        str(out_mp4),
    ]
    proc = subprocess.run(cmd, cwd=str(Path(ass_path).parent), capture_output=True, text=True)
    if proc.returncode != 0:
        die(f"ffmpeg render failed:\n{proc.stderr[-1200:]}")
    # Instagram's transcoder rejects non-44.1/48 kHz audio tracks outright
    # ("both HD and SD progressive failed to transcode"), so verify before uploading.
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=sample_rate,channels", "-of", "csv=p=0", str(out_mp4)],
        capture_output=True, text=True,
    )
    rate = (probe.stdout or "").strip().split(",")[0]
    if rate not in ("44100", "48000"):
        die(f"rendered audio sample rate is {rate or 'unknown'} Hz (must be 44100/48000) — Instagram would reject it")
    log(f"audio track: {rate} Hz (IG-compatible)")
    for line in (proc.stderr or "").splitlines():
        if "fontselect" in line:
            log(line.strip())


# ---------------------------------------------------------------------------
# Step D — Instagram Audio API (/ig_audio) instrumental music selection
# ---------------------------------------------------------------------------

GOOD_AUDIO_WORDS = ["instrumental", "piano", "ambient", "cello", "strings", "calm", "dark", "slow",
                    "melanchol", "soothing", "minimal", "sleep", "sad", "moody", "cinematic",
                    "acoustic", "study", "lofi", "lo-fi", "peaceful", "reverie", "classical",
                    "etude", "gentle", "relax", "meditation", "dreamy"]
BAD_AUDIO_WORDS = ["vocal", "voice", "feat", "remix", "cover", "karaoke", "sped up", "nightcore",
                   "party", "dance", "club", "edm", "rap", "hip hop", "pop ", "upbeat", "celebrat",
                   "hype", "trap"]


def _all_strings(obj, depth=0):
    parts = []
    if depth > 3:
        return parts
    if isinstance(obj, dict):
        for v in obj.values():
            parts += _all_strings(v, depth + 1)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            parts += _all_strings(v, depth + 1)
    elif isinstance(obj, str):
        parts.append(obj)
    return parts


def _collect_candidates(obj):
    found, seen = [], set()

    def walk(o):
        if isinstance(o, dict):
            aid = o.get("audio_id")
            if aid and str(aid) not in seen:
                seen.add(str(aid))
                found.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, (list, tuple)):
            for v in o:
                walk(v)

    walk(obj)
    return found


def candidate_score(candidate) -> int:
    text = " ".join(_all_strings(candidate)).lower()
    return sum(2 for w in GOOD_AUDIO_WORDS if w in text) - sum(3 for w in BAD_AUDIO_WORDS if w in text)


def candidate_title(candidate) -> str:
    for key in ("title", "audio_title", "name", "display_name", "track_name", "song_title"):
        value = candidate.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for s in _all_strings(candidate):
        if 3 <= len(s) <= 120 and not s.lower().startswith("http") and not s.isdigit():
            return s.strip()
    return ""


def pick_audio_candidates(user_id, token, queries):
    """Mood queries -> strictly instrumental/calm/dark candidates (score > 0).

    Some accounts receive a fixed 'eligible trending' list regardless of the
    query — the loop detects an identical repeated result set and stops early
    instead of wasting calls. If nothing clearly matches the
    instrumental/calm/dark criteria, return [] so the caller publishes without
    background music rather than attaching something off-brief.
    """
    picked = {}
    last_idset = None
    for query in queries[:4]:
        try:
            payload = graph_get("/ig_audio", {"audio_type": "music", "q": query, "user_id": user_id}, token=token)
        except GraphError as e:
            log(f"ig_audio query '{query}' failed: {_short(e)}")
            continue
        batch = _collect_candidates(payload)
        for cand in batch:
            picked.setdefault(str(cand.get("audio_id")), cand)
        id_set = {str(c.get("audio_id")) for c in batch}
        if id_set and id_set == last_idset:
            log(f"ig_audio: query '{query}' returned the same fixed list — stopping")
            break
        last_idset = id_set
        if any(candidate_score(c) > 0 for c in picked.values()):
            log(f"ig_audio: instrumental match found via query '{query}'")
            break
    ranked = sorted(picked.values(), key=candidate_score, reverse=True)
    eligible = [c for c in ranked if candidate_score(c) > 0]
    if not eligible and picked:
        log("ig_audio: no clearly instrumental/calm track in the eligible list — publishing without background music")
    return [{"audio_id": str(c.get("audio_id")), "title": candidate_title(c), "score": candidate_score(c)}
            for c in eligible[:4]]


# ---------------------------------------------------------------------------
# Step E — container, resumable upload, publish
# ---------------------------------------------------------------------------


def _is_audio_rejection(e: GraphError) -> bool:
    if getattr(e, "subcode", None) == 2207065:
        return True
    s = str(e).lower()
    return "audio" in s and any(w in s for w in ("licens", "restrict", "not available", "not allowed", "invalid", "unsupported"))


def create_container(user_id, token, caption, share_to_feed, audio_id=None, thumb_offset=2000, location_id=None, video_url=None):
    params = {
        "media_type": "REELS",
        "caption": caption,
        "share_to_feed": "true" if share_to_feed else "false",
    }
    if video_url:
        # Instagram pulls the file itself; no resumable upload step involved.
        params["video_url"] = video_url
    else:
        params["upload_type"] = "resumable"
    if thumb_offset is not None:
        params["thumb_offset"] = str(int(thumb_offset))
    if location_id:
        params["location_id"] = str(location_id)
    if audio_id:
        params["audio_configuration"] = json.dumps({"audio_id": str(audio_id), "audio_volume": 20})
    resp = graph_post(f"/{user_id}/media", params, token=token)
    container_id = resp.get("id")
    if not container_id:
        raise GraphError(200, {"error": {"message": "container creation returned no id", "payload": resp}})
    return container_id


def create_container_smart(user_id, token, caption, share_to_feed, audio_candidates, thumb_offset, location_id=None, video_url=None):
    """Container with resilient music fallback: up to 2 audio tries, then no music."""
    for cand in (audio_candidates or [])[:2]:
        try:
            container_id = create_container(user_id, token, caption, share_to_feed,
                                            audio_id=cand["audio_id"], thumb_offset=thumb_offset,
                                            location_id=location_id, video_url=video_url)
            log(f"container created with audio '{cand['title'] or cand['audio_id']}' (audio_id={cand['audio_id']})")
            return container_id, cand
        except GraphError as e:
            if _is_audio_rejection(e):
                log(f"audio_id {cand['audio_id']} rejected by Meta ({_short(e)}) — trying next candidate")
                continue
            raise
    if audio_candidates:
        log("no acceptable background audio — falling back to container WITHOUT audio_configuration")
    try:
        container_id = create_container(user_id, token, caption, share_to_feed,
                                        audio_id=None, thumb_offset=thumb_offset, location_id=location_id,
                                        video_url=video_url)
        return container_id, None
    except GraphError as e:
        if "thumb" in str(e).lower():
            log("thumb_offset rejected — retrying container without it")
            container_id = create_container(user_id, token, caption, share_to_feed,
                                            audio_id=None, thumb_offset=None, location_id=location_id,
                                            video_url=video_url)
            return container_id, None
        raise


class UploadFailed(Exception):
    """Resumable upload rejected by Meta (transient ProcessingFailedError etc.)."""


def upload_video(container_id, token, mp4_path, attempts=3):
    size = Path(mp4_path).stat().st_size
    url = f"{RUPLOAD}/{container_id}"
    with open(mp4_path, "rb") as fh:
        raw = fh.read()
    last = ""
    for i in range(max(1, attempts)):
        headers = {
            "Authorization": f"OAuth {token}",
            "offset": "0",
            "file_size": str(size),
            "Content-Type": "application/octet-stream",
        }
        status, content = http_request("POST", url, headers=headers, body=raw, timeout=1200)
        if 200 <= status < 300:
            log(f"resumable upload: {size / 1e6:.1f} MB -> http {status}")
            return
        last = f"http {status}: {_short(content, 300)}"
        log(f"resumable upload attempt {i + 1}/{attempts} failed ({last})")
        if i + 1 < attempts:
            time.sleep(3 * (i + 1))
    raise UploadFailed(last)


def create_and_upload(user_id, token, mp4, caption, share_to_feed, audio_candidates,
                      thumb_offset, location_id=None, rounds=3):
    """Create a container and upload; on rejection recreate the container and retry.

    Meta's rupload endpoint intermittently returns
    {"type":"ProcessingFailedError","retriable":false} for perfectly valid files;
    a fresh container + retry is the pragmatic fix.
    """
    last = ""
    for r in range(max(1, rounds)):
        container_id, used_audio = create_container_smart(
            user_id, token, caption, share_to_feed, audio_candidates, thumb_offset, location_id)
        log(f"container: {container_id}" + (f" (round {r + 1}/{rounds})" if r else ""))
        try:
            upload_video(container_id, token, mp4)
            return container_id, used_audio
        except UploadFailed as e:
            last = str(e)
            log(f"upload rejected on container {container_id} — retrying with a fresh container")
            time.sleep(3 * (r + 1))
    die(f"resumable upload failed after {rounds} containers: {last}")


def _free_port(start=8090, tries=60):
    import socket
    for p in range(start, start + tries):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise RuntimeError("no free port available")


class LocalHoster:
    """Serve a directory over HTTP and publish it with `agent37 expose`.

    Used for the video_url ingest path: Instagram downloads the render itself,
    which bypasses the resumable-upload endpoint entirely.
    """

    def __init__(self, directory):
        self.directory = str(directory)
        self.proc = None
        self.port = None
        self.url = None

    def __enter__(self):
        self.port = _free_port()
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(self.port), "--bind", "0.0.0.0"],
            cwd=self.directory, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=2):
                    break
            except Exception:
                time.sleep(0.3)
        out = subprocess.run(["agent37", "expose", str(self.port), "--label", "reel-ingest"],
                             capture_output=True, text=True, timeout=180)
        payload = None
        for line in (out.stdout or "").splitlines():
            line = line.strip()
            if line.startswith("{") and '"url"' in line:
                try:
                    payload = json.loads(line)
                except Exception:
                    continue
        if not payload or not payload.get("url"):
            self._stop()
            die(f"agent37 expose failed for port {self.port}: {(out.stdout or '')[-300:]}{(out.stderr or '')[-300:]}")
        self.url = payload["url"].rstrip("/")
        log(f"public ingest URL: {self.url}")
        return self

    def _stop(self):
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.kill()
            self.proc = None

    def __exit__(self, *exc):
        if self.port:
            subprocess.run(["agent37", "unexpose", str(self.port)], capture_output=True, text=True, timeout=120)
        self._stop()
        return False


def publish_via_url(user_id, token, mp4_path, caption, share_to_feed, audio_candidates,
                    thumb_offset, location_id=None):
    """Create the container with video_url so Instagram fetches the render itself."""
    mp4_path = Path(mp4_path)
    with LocalHoster(mp4_path.parent) as hoster:
        video_url = f"{hoster.url}/{urllib.parse.quote(mp4_path.name)}"
        log(f"handing video_url to Instagram: {video_url}")
        container_id, used_audio = create_container_smart(
            user_id, token, caption, share_to_feed, audio_candidates,
            thumb_offset, location_id, video_url=video_url)
        log(f"container: {container_id}")
        wait_finished(container_id, token)
        return container_id, used_audio


def wait_finished(container_id, token, timeout_s=900, interval=5):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            state = graph_get(f"/{container_id}", {"fields": "status_code,status"}, token=token)
        except GraphError as e:
            log(f"container poll error (retrying): {_short(e)}")
            time.sleep(interval)
            continue
        code = state.get("status_code")
        log(f"container status: {code}")
        if code == "FINISHED":
            return state
        if code in ("ERROR", "EXPIRED"):
            die(f"container processing failed: {json.dumps(state)}")
        time.sleep(interval)
    die("container processing timed out")


def publish(user_id, token, container_id):
    resp = graph_post(f"/{user_id}/media_publish", {"creation_id": container_id}, token=token)
    media_id = resp.get("id")
    if not media_id:
        die(f"media_publish returned no id: {json.dumps(resp)}")
    return media_id


def fetch_permalink(media_id, token):
    try:
        data = graph_get(f"/{media_id}", {"fields": "permalink"}, token=token)
        return data.get("permalink") or ""
    except GraphError as e:
        log(f"permalink fetch failed: {_short(e)}")
        return ""


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def cleanup_stale():
    """Delete stale temp workspaces from crashed previous runs (best effort)."""
    base = Path(tempfile.gettempdir())
    cutoff = time.time() - 6 * 3600
    for d in base.glob("makethevideoandpost-*"):
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser(description="Minimalist audio -> Instagram Reel pipeline (1080p, no b-rolls).")
    parser.add_argument("audio", nargs="?", help="Audio file path (.m4a/.mp3/.wav/.ogg)")
    parser.add_argument("--dry-run", action="store_true", help="Transcribe + render only; never uploads or publishes.")
    parser.add_argument("--caption", default=None, help="Override the auto-generated caption.")
    parser.add_argument("--location", default=None, help="Place name to attach via location_id when found.")
    parser.add_argument("--keep-temp", action="store_true", help="Keep temporary render files (debugging).")
    parser.add_argument("--upload-mode", choices=["auto", "resumable", "url"], default="auto",
                        help="auto = try resumable upload, then fall back to video_url ingest (default).")
    parser.add_argument("--check-auth", action="store_true", help="Verify IG creds (GET /<IG_USER_ID>?fields=id,username) and exit.")
    args = parser.parse_args()

    load_dotenv()
    cleanup_stale()

    user_id = os.environ.get("IG_USER_ID", "").strip()
    token = os.environ.get("IG_ACCESS_TOKEN", "").strip()

    if args.check_auth:
        if not user_id or not token:
            die("IG_USER_ID / IG_ACCESS_TOKEN missing in ~/.hermes/.env")
        account = graph_get(f"/{user_id}", {"fields": "id,username"}, token=token)
        print(json.dumps({"ok": True, "account": account}, indent=2))
        return

    if not args.audio:
        die("audio file path required (usage: makethevideoandpost.py <audio> [--dry-run])")
    audio = Path(args.audio).expanduser().resolve()
    if not audio.exists() or not audio.is_file():
        die(f"audio file not found: {audio}")
    if not user_id or not token:
        die("IG_USER_ID / IG_ACCESS_TOKEN missing in ~/.hermes/.env")

    started = time.time()
    duration = probe_duration(audio)
    log(f"audio: {audio.name} — {duration:.2f}s (original file passed as-is; never converted)")

    transcript = transcribe(audio)
    words, text = transcript["words"], transcript["text"]
    if not words:
        die("no word timestamps available from STT")
    log(f"transcript: {len(words)} words via {transcript['source']}")

    ctx = fetch_account_context(user_id, token)
    plan = decide(duration, text, ctx)
    log(f"plan: {plan['aspect']} {plan['res'][0]}x{plan['res'][1]} | font={plan['font']}@{plan['size']} | "
        f"share_to_feed={plan['share_to_feed']} | mood={plan['mood']}")
    log(f"plan reason: {plan['reason']}")
    caption = args.caption.strip() if args.caption else build_caption(text, ctx, plan["mood"])

    location_id = None
    if args.location:
        try:
            found = graph_get("/pages/search", {"q": args.location, "fields": "id,name"}, token=token)
            first = (found.get("data") or [{}])[0]
            if first.get("id"):
                location_id = first["id"]
                log(f"location attached: {first.get('name')} (id={location_id})")
        except GraphError as e:
            log(f"location lookup failed — continuing without it ({_short(e)})")

    thumb_offset = 2000
    if duration * 1000 < 2600:
        thumb_offset = max(0, int(duration * 1000 * 0.5))

    tmpdir = Path(tempfile.mkdtemp(prefix="makethevideoandpost-"))
    result = {
        "ok": True,
        "dry_run": bool(args.dry_run),
        "permalink": None,
        "media_id": None,
        "aspect_ratio": plan["aspect"],
        "resolution": f"{plan['res'][0]}x{plan['res'][1]}",
        "aspect_reason": plan["reason"],
        "font": plan["font"],
        "font_size": plan["size"],
        "share_to_feed": plan["share_to_feed"],
        "mood": plan["mood"],
        "stt_source": transcript["source"],
        "duration_s": round(duration, 2),
        "words": len(words),
        "caption": caption,
        "music": {"attached": False, "audio_id": None, "title": ""},
        "account_median_likes": ctx.get("median_likes"),
        "insights_pulled": len(ctx.get("insights") or {}),
    }
    try:
        cues = group_words(words)
        ass_path = tmpdir / "subs.ass"
        build_ass(cues, plan["res"], plan["font"], plan["size"], ass_path)
        log(f"subtitles: {len(cues)} cues")
        mp4 = tmpdir / "reel.mp4"
        render_video(audio, ass_path, mp4, plan["res"], duration)
        log(f"rendered: {mp4.stat().st_size / 1e6:.1f} MB ({plan['res'][0]}x{plan['res'][1]}, 30fps, crf18)")
        result["cues"] = len(cues)

        if not args.dry_run:
            audio_candidates = pick_audio_candidates(user_id, token, plan["music_queries"])
            if audio_candidates:
                log("audio candidates: " + ", ".join(f"{c['title'] or c['audio_id']} (score {c['score']})" for c in audio_candidates))
            container_id = used_audio = None
            if args.upload_mode in ("auto", "resumable"):
                try:
                    container_id, used_audio = create_and_upload(
                        user_id, token, mp4, caption, plan["share_to_feed"],
                        audio_candidates, thumb_offset, location_id,
                        rounds=3 if args.upload_mode == "resumable" else 1)
                except SystemExit:
                    if args.upload_mode == "resumable":
                        raise
                    log("resumable upload path rejected the render — falling back to video_url ingest")
            if container_id is None:
                container_id, used_audio = publish_via_url(
                    user_id, token, mp4, caption, plan["share_to_feed"],
                    audio_candidates, thumb_offset, location_id)
                result["upload_mode"] = "video_url"
            else:
                result["upload_mode"] = "resumable"
                log("waiting for container processing…")
                wait_finished(container_id, token)
            media_id = publish(user_id, token, container_id)
            permalink = fetch_permalink(media_id, token)
            result["media_id"] = media_id
            result["permalink"] = permalink
            if used_audio:
                result["music"] = {"attached": True, "audio_id": used_audio["audio_id"], "title": used_audio["title"]}
            log(f"PUBLISHED: {permalink}")
        else:
            log("dry-run: skipping Instagram upload/publish")

        permalink_line = result.get("permalink") or ("(dry-run)" if args.dry_run else "(unavailable)")
        print("PERMALINK: " + permalink_line)
        print("RESULT_JSON: " + json.dumps(result, ensure_ascii=False))
    finally:
        if args.keep_temp:
            log(f"temp files kept at {tmpdir}")
        else:
            shutil.rmtree(tmpdir, ignore_errors=True)
            log("temporary audio/video/subtitle files deleted")
    log(f"done in {time.time() - started:.1f}s")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[mtvap] interrupted", file=sys.stderr)
        raise SystemExit(130)
    except GraphError as e:
        print(f"[mtvap][ERROR] Instagram API: {e}", file=sys.stderr)
        raise SystemExit(1)
