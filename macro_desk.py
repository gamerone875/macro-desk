"""
Macro Desk - automatic digest of Bloomberg Television (YouTube) for trading.

Pipeline (runs on GitHub Actions, free):
  1. List recent uploads of the Bloomberg Television channel (@markets) with the
     YouTube Data API (free key).
  2. Add any video links sent manually (workflow input or links.txt).
  3. Send each video URL directly to Gemini (free tier) which watches/listens to
     it and returns structured notes (English).
  4. Build a "desk brief" from the last 24h of notes.
  5. Render docs/index.html (served by GitHub Pages).

Everything is configurable through environment variables (see CONFIG below).
"""

import datetime as dt
import html
import json
import os
import re
import sys
import time

import requests
from google import genai
from google.genai import errors, types

# --------------------------------------------------------------------------- #
# CONFIG
# --------------------------------------------------------------------------- #
ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
DOCS_DIR = os.path.join(ROOT, "docs")
VIDEOS_FILE = os.path.join(DATA_DIR, "videos.json")
BRIEF_FILE = os.path.join(DATA_DIR, "brief.json")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
LINKS_FILE = os.path.join(ROOT, "links.txt")
TEMPLATE_FILE = os.path.join(ROOT, "dashboard_template.html")
OUTPUT_FILE = os.path.join(DOCS_DIR, "index.html")


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()
MANUAL_URL = os.environ.get("MANUAL_URL", "").strip()
REMOVE_URL = os.environ.get("REMOVE_URL", "").strip()  # links/IDs of videos to delete from the desk
AUTO_CHANNEL = os.environ.get("AUTO_CHANNEL", "").strip().lower() in ("1", "true", "yes")  # off: only links you send are analysed
FORCE = os.environ.get("FORCE", "").strip().lower() in ("1", "true", "yes")  # re-analyse manual links already on the desk

CHANNEL_HANDLE = os.environ.get("CHANNEL_HANDLE", "markets")  # Bloomberg Television
# Models tried in order. If one is unavailable, overloaded or out of quota,
# the next one is used. Paid tier, mixed setup:
#  - video parts (the bulk of the cost): cheap Flash-Lite models
#  - merging parts + the daily brief (small, text only): the stronger Flash model
def _models(name, default):
    return [m.strip() for m in os.environ.get(name, default).split(",") if m.strip()]

VIDEO_MODELS = _models("GEMINI_VIDEO_MODELS",
    "gemini-3.1-flash-lite,gemini-3.1-flash-lite-preview,gemini-2.5-flash-lite,gemini-3.8-flash")
TEXT_MODELS = _models("GEMINI_TEXT_MODELS",
    "gemini-3.8-flash,gemini-3.1-flash-lite,gemini-2.5-flash-lite")  # Lite as backup: never lose analysed parts
GEMINI_MODELS = TEXT_MODELS  # kept for compatibility

LOOKBACK_HOURS = env_int("LOOKBACK_HOURS", 36)          # how far back to look for new replays
MIN_MINUTES = env_int("MIN_MINUTES", 40)                 # skip shorts / tiny clips
MAX_MINUTES_PER_VIDEO = env_int("MAX_MINUTES_PER_VIDEO", 160)  # longer videos: first N minutes only
SKIP_LONGER_THAN = env_int("SKIP_LONGER_THAN", 300)     # skip 24/7 stream archives (unless sent manually)
MAX_MINUTES_PER_RUN = env_int("MAX_MINUTES_PER_RUN", 300)
DAILY_MINUTES_CAP = env_int("DAILY_MINUTES_CAP", 900)   # cost guard on the paid tier (~15 h of video / day)
CHUNK_MINUTES = env_int("CHUNK_MINUTES", 30)            # one Gemini call per chunk
FPS = env_float("VIDEO_FPS", 0.05)                      # 1 frame / 20 s: audio is what matters
PACE_SECONDS = env_int("PACE_SECONDS", 5)               # pause between video calls (paid tier: higher limits)
KEEP_DAYS = env_int("KEEP_DAYS", 21)
BUSY_ROUNDS = env_int("BUSY_ROUNDS", 3)              # extra passes over all models when Gemini is overloaded (503)
BRIEF_WINDOW_HOURS = env_int("BRIEF_WINDOW_HOURS", 24)

FOCUS = ("Nasdaq 100, S&P 500, gold and crude oil (WTI/Brent); also note what matters "
         "for them: Fed and rates, US data, USD, earnings, geopolitics, OPEC")

YT_API = "https://www.googleapis.com/youtube/v3"
VIDEO_ID_RE = re.compile(r"(?:v=|youtu\.be/|/live/|/shorts/|/embed/)([A-Za-z0-9_-]{11})")


def log(*args):
    print(*args, flush=True)


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def iso(d):
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def pacific_day():
    # Gemini daily quotas reset at midnight Pacific time (approx. UTC-8/-7).
    return (now_utc() - dt.timedelta(hours=8)).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# YouTube Data API
# --------------------------------------------------------------------------- #
def yt_get(endpoint, **params):
    params["key"] = YOUTUBE_API_KEY
    r = requests.get(f"{YT_API}/{endpoint}", params=params, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"YouTube API {endpoint} -> {r.status_code}: {r.text[:300]}")
    return r.json()


def iso_duration_minutes(s):
    m = re.match(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return 0.0
    d, h, mi, se = (int(x) if x else 0 for x in m.groups())
    return d * 1440 + h * 60 + mi + se / 60


def uploads_playlist_id():
    data = yt_get("channels", part="contentDetails", forHandle=CHANNEL_HANDLE)
    items = data.get("items") or []
    if not items:
        raise RuntimeError(f"Channel @{CHANNEL_HANDLE} not found")
    return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]


def recent_upload_ids(since):
    playlist = uploads_playlist_id()
    data = yt_get("playlistItems", part="contentDetails", playlistId=playlist, maxResults=50)
    ids = []
    for it in data.get("items", []):
        cd = it.get("contentDetails", {})
        published = cd.get("videoPublishedAt")
        if published and parse_iso(published) >= since:
            ids.append(cd["videoId"])
    return ids


def video_details(ids):
    out = {}
    for i in range(0, len(ids), 50):
        data = yt_get("videos", part="snippet,contentDetails,liveStreamingDetails",
                      id=",".join(ids[i:i + 50]))
        for it in data.get("items", []):
            sn = it["snippet"]
            thumbs = sn.get("thumbnails", {})
            thumb = (thumbs.get("medium") or thumbs.get("high") or thumbs.get("default") or {}).get("url", "")
            out[it["id"]] = {
                "id": it["id"],
                "title": sn.get("title", ""),
                "channel": sn.get("channelTitle", ""),
                "published": sn.get("publishedAt", ""),
                "live": sn.get("liveBroadcastContent", "none"),  # live / upcoming / none
                "minutes": round(iso_duration_minutes(it["contentDetails"].get("duration")), 1),
                "thumbnail": thumb,
                "url": f"https://www.youtube.com/watch?v={it['id']}",
            }
    return out


# --------------------------------------------------------------------------- #
# Manual links
# --------------------------------------------------------------------------- #
def manual_ids():
    ids = []
    sources = [MANUAL_URL]
    if os.path.exists(LINKS_FILE):
        with open(LINKS_FILE, encoding="utf-8") as f:
            sources += [l.strip() for l in f if l.strip() and not l.strip().startswith("#")]
    for s in sources:
        for m in VIDEO_ID_RE.finditer(s or ""):
            if m.group(1) not in ids:
                ids.append(m.group(1))
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", s or "") and s not in ids:
            ids.append(s)
    return ids


def clear_links_file():
    header = ("# Colle ici des liens YouTube (un par ligne). Ils seront analysés à la\n"
              "# prochaine mise à jour puis retirés automatiquement de ce fichier.\n")
    with open(LINKS_FILE, "w", encoding="utf-8") as f:
        f.write(header)


# --------------------------------------------------------------------------- #
# Gemini
# --------------------------------------------------------------------------- #
NOTES_SHAPE = """{
  "headline": "one line, max 15 words, the single most market-relevant point",
  "summary": "3-5 sentences: what was discussed and why it matters for markets",
  "key_takeaways": ["3-8 bullet points, most important first"],
  "macro": ["Fed / rates / inflation / growth / labor / fiscal / geopolitics points"],
  "priced_in": ["what the market is pricing or expecting: rate-cut odds, positioning, consensus, surprises"],
  "fundamentals": ["earnings, guidance, sectors, company-level or commodity supply/demand points"],
  "assets": [{"asset": "Nasdaq | S&P 500 | Gold | Oil | Rates | USD | Other: <name>",
              "bias": "bullish | bearish | neutral | mixed",
              "view": "one sentence",
              "source": "speaker name and firm, or 'anchors'"}],
  "guests": [{"name": "", "role": "title and firm", "key_view": "one sentence"}],
  "catalysts": ["upcoming events / data / dates mentioned"],
  "sentiment": "risk-on | risk-off | mixed | neutral"
}"""

RULES = (
    "Rules: write in English. Be factual and concise. Attribute views to the person who "
    "expressed them. Never invent numbers, names or levels; if something was not said, leave "
    "the list empty. Ignore ads, promos and small talk. Return ONLY valid JSON."
)


def video_prompt(title, part, parts, start_min, end_min):
    where = f" (part {part}/{parts}, minutes {start_min}-{end_min})" if parts > 1 else ""
    return (
        f"You are the assistant of a macro trader. Watch this Bloomberg Television video "
        f"titled \"{title}\"{where} and extract what matters for a trader focused on {FOCUS}.\n"
        f"{RULES}\nJSON shape:\n{NOTES_SHAPE}"
    )


MERGE_PROMPT = (
    "Below are JSON notes taken on consecutive parts of the same Bloomberg Television video "
    "titled \"{title}\". Merge them into ONE set of notes with exactly the same JSON shape: "
    "deduplicate, keep the most market-relevant points first, keep attributions.\n"
    f"{RULES}\nJSON shape:\n{NOTES_SHAPE}\n\nNotes:\n{{notes}}"
)

BRIEF_SHAPE = """{
  "narrative": "4-6 sentences: the story the market is trading right now, according to these shows",
  "regime": "risk-on | risk-off | mixed | neutral",
  "assets": {
    "Nasdaq":  {"bias": "bullish | bearish | neutral | mixed", "why": "1-2 sentences"},
    "S&P 500": {"bias": "...", "why": "..."},
    "Gold":    {"bias": "...", "why": "..."},
    "Oil":     {"bias": "...", "why": "..."}
  },
  "priced_in": ["what is priced in / consensus expectations"],
  "debates": ["where guests disagree or contrarian views worth knowing"],
  "watch_next": ["upcoming catalysts with dates if known"]
}"""

BRIEF_PROMPT = (
    "You are the head of a macro trading desk. Below are structured notes from the latest "
    "Bloomberg Television shows. Write the desk brief for a trader focused on " + FOCUS + ". "
    "Base everything strictly on the notes; if an asset was not discussed, use bias 'neutral' "
    "and say it was not covered.\n" + RULES + "\nJSON shape:\n" + BRIEF_SHAPE + "\n\nNotes:\n{notes}"
)


class QuotaExhausted(Exception):
    pass


_client = None


def client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client


def parse_json_text(text):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise


def is_network_error(e):
    """Dropped connection / timeout, whatever HTTP library google-genai uses."""
    if isinstance(e, (ConnectionError, TimeoutError, requests.exceptions.RequestException)):
        return True
    name = type(e).__name__.lower()
    msg = str(e).lower()
    return any(k in name for k in ("disconnect", "protocolerror", "timeout", "connecterror",
                                   "connectionerror", "readerror", "transporterror")) \
        or any(k in msg for k in ("server disconnected", "connection reset", "timed out",
                                  "connection aborted", "remote end closed"))


def retry_delay(msg, default=65):
    """Use the delay Gemini suggests in its 429 message (e.g. 'retry in 12.4s')."""
    m = re.search(r"retry(?:Delay)?\D{0,12}(\d+(?:\.\d+)?)\s*s", msg, re.I)
    if m:
        return max(5, min(default, int(float(m.group(1))) + 2))
    return default


def gemini_json(contents, video=False, models=None):
    """Call Gemini, trying each model in turn. Returns (dict, model_name).

    When every model answers 503 (overloaded), the whole list is tried again
    after a pause (1, 2, then 4 min) instead of giving up: these overloads on
    the free tier are usually spikes that pass within a few minutes.
    """
    cfg = dict(response_mime_type="application/json", temperature=0.3)
    if video:
        cfg["media_resolution"] = types.MediaResolution.MEDIA_RESOLUTION_LOW
    models = models or (VIDEO_MODELS if video else TEXT_MODELS)
    last_err = None
    exhausted = set()  # models whose daily quota is used up
    for rnd in range(BUSY_ROUNDS + 1):
        busy = False
        for model in models:
            if model in exhausted:
                continue
            for attempt in range(3):
                try:
                    resp = client().models.generate_content(
                        model=model, contents=contents,
                        config=types.GenerateContentConfig(**cfg))
                    return parse_json_text(resp.text), model
                except errors.APIError as e:
                    last_err = e
                    msg = str(e)
                    code = getattr(e, "code", None)
                    if code == 429:
                        if "PerDay" in msg or "per day" in msg.lower() or "daily" in msg.lower():
                            log(f"  {model}: daily quota reached, trying next model")
                            exhausted.add(model)
                            break
                        wait = retry_delay(msg)
                        log(f"  {model}: rate limited, waiting {wait} s")
                        time.sleep(wait)
                        continue
                    if code == 404 or code in (400, 403) and any(k in msg.lower() for k in (
                            "not found", "not supported", "unsupported", "is not available", "permission")):
                        log(f"  {model}: unavailable ({code}), trying next model")
                        exhausted.add(model)
                        break
                    if code and code >= 500:
                        log(f"  {model}: Gemini busy ({code}), trying next model")
                        busy = True
                        break
                    raise
                except (json.JSONDecodeError, ValueError) as e:
                    last_err = e
                    log(f"  {model}: invalid JSON, retrying")
                    continue
                except Exception as e:
                    if not is_network_error(e):
                        raise
                    # "Server disconnected without sending a response", timeouts...
                    last_err = e
                    log(f"  {model}: connection dropped ({type(e).__name__}), trying next model")
                    busy = True
                    break
        if not busy or rnd == BUSY_ROUNDS or len(exhausted) == len(models):
            break
        wait = 60 * 2 ** rnd  # 1, 2, 4 min
        log(f"  all models busy or disconnected, waiting {wait // 60} min before trying again ({rnd + 1}/{BUSY_ROUNDS})")
        time.sleep(wait)
    raise QuotaExhausted(f"All models failed. Last error: {last_err}")


def summarize_video(v):
    total = min(v["minutes"], MAX_MINUTES_PER_VIDEO)
    n = max(1, int(-(-total // CHUNK_MINUTES)))  # ceil
    size = total / n
    parts = []
    model_used = None
    for i in range(n):
        start = int(i * size * 60)
        end = int(min(total, (i + 1) * size) * 60)
        log(f"  part {i + 1}/{n}: {start // 60}-{end // 60} min")
        contents = types.Content(parts=[
            types.Part(
                file_data=types.FileData(file_uri=v["url"]),
                video_metadata=types.VideoMetadata(
                    start_offset=f"{start}s", end_offset=f"{end}s", fps=FPS),
            ),
            types.Part(text=video_prompt(v["title"], i + 1, n, start // 60, end // 60)),
        ])
        notes, model_used = gemini_json(contents, video=True)
        parts.append(notes)
        if i < n - 1:  # no pause after the last part
            time.sleep(PACE_SECONDS)
    if len(parts) == 1:
        notes = parts[0]
    else:
        prompt = MERGE_PROMPT.replace("{title}", v["title"]).replace(
            "{notes}", json.dumps(parts, ensure_ascii=False))
        notes, model_used = gemini_json(prompt)
    notes["covered_minutes"] = round(total)
    notes["partial"] = v["minutes"] > MAX_MINUTES_PER_VIDEO
    return notes, model_used, total


def build_brief(videos):
    cutoff = now_utc() - dt.timedelta(hours=BRIEF_WINDOW_HOURS)
    recent = [v for v in videos if parse_iso(v["published"]) >= cutoff] or videos[:6]
    if not recent:
        return None
    compact = [{
        "title": v["title"], "published": v["published"],
        **{k: v["notes"].get(k) for k in ("headline", "key_takeaways", "macro", "priced_in",
                                          "fundamentals", "assets", "catalysts", "sentiment")}
    } for v in recent[:12]]
    brief, model = gemini_json(BRIEF_PROMPT.replace("{notes}", json.dumps(compact, ensure_ascii=False)))
    brief["generated_at"] = iso(now_utc())
    brief["based_on"] = [v["id"] for v in recent[:12]]
    brief["model"] = model
    return brief


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #
def render(videos, brief, state):
    with open(TEMPLATE_FILE, encoding="utf-8") as f:
        tpl = f.read()
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    payload = {
        "generated_at": iso(now_utc()),
        "brief": brief,
        "videos": videos,
        "update_url": "",  # buttons hidden on the public page
        "links_url": "",
        "usage": state.get("usage", {}),
        "daily_cap": DAILY_MINUTES_CAP,
        "last_errors": [],  # warnings stay in the Actions log and state.json, not on the public page
    }
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    os.makedirs(DOCS_DIR, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(tpl.replace("/*__DATA__*/null", data))
    # GitHub Pages: serve files as-is
    open(os.path.join(DOCS_DIR, ".nojekyll"), "w").close()


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    if "--render-only" in sys.argv:
        render(load_json(VIDEOS_FILE, []), load_json(BRIEF_FILE, None), load_json(STATE_FILE, {}))
        log("Dashboard rendered.")
        return

    if not GEMINI_API_KEY or not YOUTUBE_API_KEY:
        sys.exit("Missing GEMINI_API_KEY or YOUTUBE_API_KEY (GitHub > Settings > Secrets > Actions).")

    videos = load_json(VIDEOS_FILE, [])
    brief = load_json(BRIEF_FILE, None)
    state = load_json(STATE_FILE, {})
    done = set(state.get("done", []))
    failures = state.get("failures", {})
    usage = state.get("usage", {})
    today = pacific_day()
    used_today = usage.get(today, 0)
    errors_this_run = []

    # 0) removals requested from the workflow form
    to_remove = set(VIDEO_ID_RE.findall(REMOVE_URL)) | {
        t for t in re.split(r"[\s,]+", REMOVE_URL) if re.fullmatch(r"[A-Za-z0-9_-]{11}", t)}
    for i in to_remove:
        title = next((v["title"] for v in videos if v["id"] == i), None)
        log(f"removed from the desk: {title}" if title else f"remove {i}: not on the desk")
        done.add(i)  # never re-added automatically
    videos = [v for v in videos if v["id"] not in to_remove]

    # 1) candidates
    manual = manual_ids()  # only the links sent with this run
    since = now_utc() - dt.timedelta(hours=LOOKBACK_HOURS)
    try:
        auto = recent_upload_ids(since) if AUTO_CHANNEL else []
    except Exception as e:  # keep going with manual links
        log(f"Could not list channel uploads: {e}")
        errors_this_run.append(f"YouTube listing: {e}")
        auto = []
    known = {v["id"] for v in videos}
    wanted = [i for i in manual if FORCE or i not in known] + [i for i in auto if i not in done and i not in known]
    details = video_details(wanted) if wanted else {}
    log(f"Manual links: {len(manual)} | recent uploads: {len(auto)} | new to process: {len(details)}")
    for i in manual:
        if i in known and not FORCE:
            log(f"manual link {i}: already on the desk, skipped (re-run with force=true to re-analyse)")
        elif i not in details:
            log(f"manual link {i}: not found on YouTube (private, deleted or bad link)")

    # 2) process, manual first then newest first
    queue = [details[i] for i in manual if i in details]
    queue += sorted([details[i] for i in auto if i in details and i not in manual],
                    key=lambda v: v["published"], reverse=True)
    run_minutes = 0
    processed_any = False
    for v in queue:
        is_manual = v["id"] in manual
        if v["live"] in ("live", "upcoming"):
            log(f"skip (live/upcoming): {v['title']}")
            continue
        if not is_manual and v["minutes"] < MIN_MINUTES:
            log(f"skip (short, {v['minutes']} min): {v['title']}")
            done.add(v["id"]); continue
        if not is_manual and v["minutes"] > SKIP_LONGER_THAN:
            log(f"skip (too long, {v['minutes']} min): {v['title']}")
            done.add(v["id"]); continue
        cost = min(v["minutes"], MAX_MINUTES_PER_VIDEO)
        if run_minutes + cost > MAX_MINUTES_PER_RUN and run_minutes > 0:
            log(f"run budget reached, left for next run: {v['title']}")
            continue
        if used_today + cost > DAILY_MINUTES_CAP:
            log(f"daily video budget reached, left for later: {v['title']}")
            continue
        log(f"Processing ({cost:.0f} min{', manual' if is_manual else ''}): {v['title']}")
        try:
            notes, model, minutes = summarize_video(v)
        except QuotaExhausted as e:
            log(f"Gemini quota exhausted: {e}")
            errors_this_run.append(str(e)[:300])
            break
        except Exception as e:
            log(f"  failed: {e}")
            errors_this_run.append(f"{v['title']}: {str(e)[:200]}")
            failures[v["id"]] = failures.get(v["id"], 0) + 1
            if failures[v["id"]] >= 3:
                done.add(v["id"])
            continue
        v.update({"notes": notes, "model": model, "manual": is_manual, "processed_at": iso(now_utc())})
        videos = [x for x in videos if x["id"] != v["id"]] + [v]
        done.add(v["id"])
        run_minutes += minutes
        used_today += minutes
        processed_any = True
        # save progress after each video so a crash never loses work
        usage[today] = round(used_today)
        save_json(VIDEOS_FILE, videos)
        save_json(STATE_FILE, {**state, "done": sorted(done), "failures": failures, "usage": usage})

    # 3) prune & sort
    keep_after = now_utc() - dt.timedelta(days=KEEP_DAYS)
    videos = [v for v in videos if parse_iso(v["published"]) >= keep_after or v.get("manual")]
    videos.sort(key=lambda v: v["published"], reverse=True)

    # 4) brief
    # rebuilt when new videos were added, or when a previous rebuild failed (e.g. Gemini 503)
    latest = max((v.get("processed_at", "") for v in videos), default="")
    stale = brief is None or (brief or {}).get("generated_at", "") < latest
    if (processed_any or state.get("brief_stale") or stale) and videos:
        try:
            new_brief = build_brief(videos)
            if new_brief:
                brief = new_brief
                state["brief_stale"] = False
        except Exception as e:
            log(f"Brief failed, will retry next run: {e}")
            errors_this_run.append(f"Brief: {str(e)[:200]}")
            state["brief_stale"] = True

    if manual:
        clear_links_file()
    state.pop("pending_manual", None)  # failed links are not kept: send them again if needed
    on_desk = {v["id"] for v in videos}
    not_analysed = []
    for i in manual:
        if i not in on_desk and i in details:
            log(f"manual link {i}: not analysed this run (send it again to retry)")
            not_analysed.append(i)

    usage = {k: v for k, v in usage.items() if k >= (now_utc() - dt.timedelta(days=7)).strftime("%Y-%m-%d")}
    usage[today] = round(used_today)
    state.update({
        "done": sorted(done)[-3000:], "failures": failures, "usage": usage,
        "last_run": iso(now_utc()), "last_errors": errors_this_run,
    })
    save_json(VIDEOS_FILE, videos)
    if brief:
        save_json(BRIEF_FILE, brief)
    save_json(STATE_FILE, state)
    render(videos, brief, state)
    log(f"Done. Processed {run_minutes:.0f} min of video this run, {used_today:.0f} min today.")
    if not_analysed or (errors_this_run and not processed_any):
        # results are still saved (the workflow commits with if: always()),
        # but the run shows red in GitHub so a failure is never mistaken for a success
        for e in errors_this_run:
            print(f"::error::{e[:300]}")
        sys.exit(f"{len(not_analysed)} link(s) not analysed - see errors above, then run again.")


if __name__ == "__main__":
    main()
