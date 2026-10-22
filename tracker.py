#!/usr/bin/env python3
"""
Self-Improvement Trend Radar
----------------------------
Runs once a day. Collects:
  * YouTube videos for every niche keyword (last N days, sorted by views)
  * YouTube "Trending" charts (Education / Howto & Style) filtered to the niche
  * Latest uploads from every tracked channel (seed list + auto-discovered breakouts)
  * YouTube search autocomplete suggestions (what people are typing right now)
  * Blogs, newsletters, Reddit and Google News RSS feeds
Stores a daily snapshot in SQLite so it can show 24h growth, days-since-upload,
days-on-radar, rising title keywords and channel growth, then writes dashboard.html.

Standard library only. Optional: `youtube-transcript-api` to capture video intros.
API key is read from the YT_API_KEY environment variable (never hardcode it).
"""
import argparse
import datetime as dt
import email.utils
import html
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
API = "https://www.googleapis.com/youtube/v3/"
UA = "Mozilla/5.0 (compatible; trend-radar/1.0)"
MAX_BYTES = 5_000_000
LOG: list[str] = []


def log(msg: str) -> None:
    line = f"{dt.datetime.now():%H:%M:%S} {msg}"
    LOG.append(line)
    print(line, flush=True)


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# --------------------------------------------------------------------------- HTTP
def http_get(url: str, params: dict | None = None, retries: int = 3, timeout: int = 30) -> bytes:
    if params:
        url = url + "?" + urllib.parse.urlencode(params, doseq=True)
    if not url.startswith("https://"):
        raise ValueError("Only HTTPS URLs are fetched")
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    raise ValueError("Response too large")
                return data
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403, 404):
                raise
            last = e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
        time.sleep(2 ** attempt)
    raise last  # type: ignore[misc]


class QuotaBudgetExceeded(Exception):
    pass


class YouTube:
    """Thin YouTube Data API v3 client with a daily quota budget (default quota is 10,000 units)."""
    COST = {"search": 100}

    def __init__(self, key: str, budget: int):
        self.key, self.budget, self.used = key, budget, 0

    def call(self, endpoint: str, **params):
        cost = self.COST.get(endpoint, 1)
        if self.used + cost > self.budget:
            raise QuotaBudgetExceeded(endpoint)
        params["key"] = self.key
        try:
            raw = http_get(API + endpoint, params)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")[:300]
            # Never include the request URL in errors: it contains the API key.
            raise RuntimeError(f"YouTube {endpoint}: HTTP {e.code} {body}") from None
        self.used += cost
        return json.loads(raw)


# --------------------------------------------------------------------------- DB
SCHEMA = """
CREATE TABLE IF NOT EXISTS channels(
  id TEXT PRIMARY KEY, title TEXT, handle TEXT, thumb TEXT, subs INTEGER,
  video_count INTEGER, view_count INTEGER, uploads TEXT,
  tracked INTEGER DEFAULT 0, auto INTEGER DEFAULT 0, first_seen TEXT, last_seen TEXT);
CREATE TABLE IF NOT EXISTS channel_stats(
  channel_id TEXT, day TEXT, subs INTEGER, views INTEGER, PRIMARY KEY(channel_id, day));
CREATE TABLE IF NOT EXISTS videos(
  id TEXT PRIMARY KEY, channel_id TEXT, title TEXT, description TEXT, published_at TEXT,
  duration_s INTEGER, thumb TEXT, tags TEXT, sources TEXT, first_seen TEXT, last_seen TEXT);
CREATE TABLE IF NOT EXISTS video_stats(
  video_id TEXT, day TEXT, views INTEGER, likes INTEGER, comments INTEGER, PRIMARY KEY(video_id, day));
CREATE TABLE IF NOT EXISTS articles(
  url TEXT PRIMARY KEY, title TEXT, source TEXT, kind TEXT, published_at TEXT, summary TEXT, first_seen TEXT);
CREATE TABLE IF NOT EXISTS keyword_stats(
  day TEXT, term TEXT, videos INTEGER, views INTEGER, PRIMARY KEY(day, term));
CREATE TABLE IF NOT EXISTS suggestions(
  day TEXT, seed TEXT, suggestion TEXT, PRIMARY KEY(day, seed, suggestion));
CREATE TABLE IF NOT EXISTS intros(video_id TEXT PRIMARY KEY, text TEXT, fetched_at TEXT);
CREATE TABLE IF NOT EXISTS handle_cache(handle TEXT PRIMARY KEY, channel_id TEXT);
CREATE TABLE IF NOT EXISTS keyword_runs(keyword TEXT PRIMARY KEY, last_day TEXT);
"""


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


# --------------------------------------------------------------------------- helpers
def iso_duration(s: str | None) -> int:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return 0
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + se


def parse_date(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    s = s.strip()
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            d = email.utils.parsedate_to_datetime(s)
        except (TypeError, ValueError):
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc)


def strip_html(s: str | None, limit: int = 280) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def chunks(seq, n):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def best_thumb(thumbs: dict) -> str:
    for k in ("maxres", "standard", "high", "medium", "default"):
        if k in thumbs:
            return thumbs[k]["url"]
    return ""


def tier_of(subs) -> str:
    if subs is None:
        return "Hidden"
    if subs < 10_000:
        return "Nano"
    if subs < 100_000:
        return "Small"
    if subs < 1_000_000:
        return "Mid"
    return "Large"


STOP = set("""a an the and or but of to in on for with your you my me i im i'm is are be been this that these those
it its it's how what why when who whom from at by as do does did not no can will just get got more most about into than so
if all out up our we they them their his her he she was were have has had like vs one two new way ways things thing
here there every day days make made should would could need want really very every don't dont never ever only still
video videos episode ep part full official""".split())


def title_terms(title: str) -> set[str]:
    words = [w.strip("'") for w in re.findall(r"[a-z0-9']+", title.lower())]
    words = [w for w in words if w]
    uni = {w for w in words if w not in STOP and len(w) > 2 and not w.isdigit()}
    bi = {f"{a} {b}" for a, b in zip(words, words[1:])
          if a not in STOP and b not in STOP and len(a) > 1 and len(b) > 1}
    return uni | bi


# --------------------------------------------------------------------------- YouTube collection
def resolve_channels(cfg, db, yt) -> None:
    """Make sure every seed channel (handle or UC… id) is in the channels table and tracked."""
    for ref in cfg.get("seed_channels", []):
        ref = ref.strip()
        cid = None
        if ref.startswith("UC") and len(ref) == 24:
            cid = ref
        else:
            handle = ref if ref.startswith("@") else "@" + ref
            row = db.execute("SELECT channel_id FROM handle_cache WHERE handle=?", (handle.lower(),)).fetchone()
            if row:
                cid = row["channel_id"]
            else:
                try:
                    d = yt.call("channels", part="id", forHandle=handle)
                    items = d.get("items", [])
                    if items:
                        cid = items[0]["id"]
                        db.execute("INSERT OR REPLACE INTO handle_cache VALUES(?,?)", (handle.lower(), cid))
                    else:
                        log(f"Seed channel not found, check the handle: {handle}")
                except QuotaBudgetExceeded:
                    raise
                except Exception as e:
                    log(f"Could not resolve {handle}: {e}")
        if cid:
            db.execute("INSERT OR IGNORE INTO channels(id, tracked) VALUES(?, 1)", (cid,))
            db.execute("UPDATE channels SET tracked=1 WHERE id=?", (cid,))
    db.commit()


def upsert_channels(db, yt, channel_ids, today) -> None:
    for batch in chunks(sorted(channel_ids), 50):
        try:
            d = yt.call("channels", part="snippet,statistics,contentDetails", id=",".join(batch), maxResults=50)
        except QuotaBudgetExceeded:
            raise
        except Exception as e:
            log(f"channels.list failed: {e}")
            continue
        for it in d.get("items", []):
            st, sn = it.get("statistics", {}), it.get("snippet", {})
            subs = None if st.get("hiddenSubscriberCount") else int(st.get("subscriberCount", 0))
            uploads = it.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
            db.execute("""INSERT INTO channels(id,title,handle,thumb,subs,video_count,view_count,uploads,first_seen,last_seen)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET title=excluded.title, handle=excluded.handle, thumb=excluded.thumb,
                  subs=excluded.subs, video_count=excluded.video_count, view_count=excluded.view_count,
                  uploads=excluded.uploads, first_seen=COALESCE(channels.first_seen, excluded.first_seen),
                  last_seen=excluded.last_seen""",
                       (it["id"], sn.get("title"), sn.get("customUrl"), best_thumb(sn.get("thumbnails", {})),
                        subs, int(st.get("videoCount", 0)), int(st.get("viewCount", 0)), uploads, today, today))
            db.execute("INSERT OR REPLACE INTO channel_stats VALUES(?,?,?,?)",
                       (it["id"], today, subs, int(st.get("viewCount", 0))))
    db.commit()


def niche_match(text: str, terms: list[str]) -> bool:
    text = text.lower()
    return any(t.lower() in text for t in terms)


def collect_youtube(cfg, db, yt, today) -> None:
    cand: dict[str, set[str]] = defaultdict(set)
    window = cfg["search_window_days"]
    after = (now_utc() - dt.timedelta(days=window)).strftime("%Y-%m-%dT%H:%M:%SZ")
    niche_terms = cfg["keywords"] + cfg.get("niche_terms", [])

    # 1) Seed channels (cheap: 1 unit each)
    resolve_channels(cfg, db, yt)

    # 2) Trending charts, filtered to the niche (1 unit each)
    for region in cfg.get("trending_regions", []):
        for cat in cfg.get("trending_categories", []):
            try:
                d = yt.call("videos", part="snippet", chart="mostPopular", regionCode=region,
                            videoCategoryId=cat, maxResults=50)
            except QuotaBudgetExceeded:
                raise
            except Exception as e:
                log(f"Trending chart {region}/{cat} unavailable: {e}")
                continue
            for it in d.get("items", []):
                sn = it.get("snippet", {})
                blob = " ".join([sn.get("title", ""), sn.get("description", "")[:500], " ".join(sn.get("tags", []))])
                if niche_match(blob, niche_terms):
                    cand[it["id"]].add(f"trending:{region}")

    # 3) Tracked channels' recent uploads (1 unit per channel)
    tracked = [r["id"] for r in db.execute("SELECT id FROM channels WHERE tracked=1")]
    missing_uploads = [r["id"] for r in db.execute("SELECT id FROM channels WHERE tracked=1 AND uploads IS NULL")]
    if missing_uploads:
        upsert_channels(db, yt, missing_uploads, today)
    upload_cutoff = now_utc() - dt.timedelta(days=cfg["channel_upload_window_days"])
    for row in db.execute("SELECT id, uploads FROM channels WHERE tracked=1 AND uploads IS NOT NULL").fetchall():
        try:
            d = yt.call("playlistItems", part="contentDetails", playlistId=row["uploads"],
                        maxResults=cfg["uploads_per_channel"])
        except QuotaBudgetExceeded:
            log("Quota budget reached while checking channels")
            break
        except Exception as e:
            log(f"Uploads for {row['id']} failed: {e}")
            continue
        for it in d.get("items", []):
            cd = it.get("contentDetails", {})
            pub = parse_date(cd.get("videoPublishedAt"))
            if pub and pub >= upload_cutoff:
                cand[cd["videoId"]].add("channel")

    # 4) Keyword searches (100 units each). Least-recently-searched keywords go first, so if the
    #    budget runs out today, the skipped keywords are first in line tomorrow. A reserve is kept
    #    for loading video and channel details afterwards.
    last = {r["keyword"]: r["last_day"] for r in db.execute("SELECT keyword, last_day FROM keyword_runs")}
    ordered = sorted(cfg["keywords"], key=lambda k: last.get(k, ""))
    reserve = cfg.get("detail_reserve_units", 800)
    for kw in ordered:
        if yt.used + yt.COST["search"] > yt.budget - reserve:
            log(f"Search budget used up; {len(ordered) - ordered.index(kw)} keywords move to tomorrow")
            break
        try:
            d = yt.call("search", part="snippet", q=kw, type="video", order="viewCount",
                        publishedAfter=after, maxResults=cfg["results_per_keyword"],
                        regionCode=cfg["region"], relevanceLanguage=cfg["language"])
        except QuotaBudgetExceeded:
            log(f"Quota budget reached; skipped keywords from '{kw}' onward")
            break
        except Exception as e:
            log(f"Search '{kw}' failed: {e}")
            continue
        for it in d.get("items", []):
            vid = it.get("id", {}).get("videoId")
            if vid:
                cand[vid].add(f"kw:{kw}")
        db.execute("INSERT OR REPLACE INTO keyword_runs VALUES(?,?)", (kw, today))
    db.commit()

    # 5) Re-check videos already on the radar so their growth keeps being measured
    recheck_cutoff = (dt.date.fromisoformat(today) - dt.timedelta(days=cfg["recheck_days"])).isoformat()
    for r in db.execute("SELECT id FROM videos WHERE last_seen>=?", (recheck_cutoff,)):
        cand.setdefault(r["id"], set())

    log(f"{len(cand)} candidate videos")

    # 6) Video details + today's stats snapshot (1 unit per 50 videos)
    channel_ids = set()
    for batch in chunks(cand.keys(), 50):
        try:
            d = yt.call("videos", part="snippet,statistics,contentDetails", id=",".join(batch), maxResults=50)
        except QuotaBudgetExceeded:
            log("Quota budget reached while loading video details")
            break
        except Exception as e:
            log(f"videos.list failed: {e}")
            continue
        for it in d.get("items", []):
            sn, st, cd = it["snippet"], it.get("statistics", {}), it.get("contentDetails", {})
            vid = it["id"]
            old = db.execute("SELECT sources FROM videos WHERE id=?", (vid,)).fetchone()
            sources = set(json.loads(old["sources"])) if old and old["sources"] else set()
            sources |= cand.get(vid, set())
            db.execute("""INSERT INTO videos(id,channel_id,title,description,published_at,duration_s,thumb,tags,sources,first_seen,last_seen)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET title=excluded.title, description=excluded.description,
                  thumb=excluded.thumb, tags=excluded.tags, sources=excluded.sources, last_seen=excluded.last_seen""",
                       (vid, sn.get("channelId"), sn.get("title"), (sn.get("description") or "")[:1500],
                        sn.get("publishedAt"), iso_duration(cd.get("duration")),
                        best_thumb(sn.get("thumbnails", {})), json.dumps(sn.get("tags", [])[:30]),
                        json.dumps(sorted(sources)), today, today))
            db.execute("INSERT OR REPLACE INTO video_stats VALUES(?,?,?,?,?)",
                       (vid, today, int(st.get("viewCount", 0)), int(st.get("likeCount", 0)),
                        int(st.get("commentCount", 0))))
            channel_ids.add(sn.get("channelId"))
    db.commit()

    # 7) Channel details for everything we saw (1 unit per 50 channels)
    upsert_channels(db, yt, {c for c in channel_ids if c}, today)


def auto_track(cfg, db, today) -> None:
    """Start following channels (often small ones) that just produced a breakout video."""
    if not cfg.get("auto_track"):
        return
    count = db.execute("SELECT COUNT(*) FROM channels WHERE tracked=1").fetchone()[0]
    room = cfg["max_tracked_channels"] - count
    if room <= 0:
        return
    rows = db.execute("""
        SELECT c.id, MAX(CAST(s.views AS REAL) / (CAST(c.view_count AS REAL) / c.video_count)) AS o
        FROM videos v JOIN video_stats s ON s.video_id=v.id AND s.day=?
        JOIN channels c ON c.id=v.channel_id
        WHERE c.tracked=0 AND c.video_count>0 AND c.view_count>0
        GROUP BY c.id HAVING o>=? ORDER BY o DESC LIMIT ?""",
                      (today, cfg["auto_track_min_outlier"], room)).fetchall()
    for r in rows:
        db.execute("UPDATE channels SET tracked=1, auto=1 WHERE id=?", (r["id"],))
    if rows:
        log(f"Auto-tracking {len(rows)} new breakout channels")
    db.commit()


# --------------------------------------------------------------------------- other sources
def collect_suggestions(cfg, db, today) -> None:
    """YouTube search autocomplete. Unofficial endpoint: may change without notice."""
    for seed in cfg.get("suggest_seeds", []):
        try:
            raw = http_get("https://suggestqueries.google.com/complete/search",
                           {"client": "firefox", "ds": "yt", "q": seed, "hl": cfg["language"]})
            try:
                data = json.loads(raw.decode("utf-8"))
            except UnicodeDecodeError:
                data = json.loads(raw.decode("latin-1"))
            for s in data[1][:15]:
                db.execute("INSERT OR IGNORE INTO suggestions VALUES(?,?,?)", (today, seed, str(s)[:120]))
        except Exception as e:
            log(f"Suggestions for '{seed}' failed: {e}")
        time.sleep(0.4)
    db.commit()


def parse_feed(raw: bytes) -> list[dict]:
    root = ET.fromstring(raw)
    out = []

    def t(node, tag):
        x = node.find(tag)
        return (x.text or "").strip() if x is not None and x.text else ""

    for it in root.iter("item"):
        out.append({"title": t(it, "title"), "link": t(it, "link"),
                    "date": t(it, "pubDate") or t(it, "{http://purl.org/dc/elements/1.1/}date"),
                    "summary": t(it, "description")})
    ns = "{http://www.w3.org/2005/Atom}"
    for e in root.iter(ns + "entry"):
        link = ""
        for l in e.findall(ns + "link"):
            if l.get("rel", "alternate") == "alternate":
                link = l.get("href", "")
                break
        out.append({"title": t(e, ns + "title"), "link": link,
                    "date": t(e, ns + "published") or t(e, ns + "updated"),
                    "summary": t(e, ns + "summary") or t(e, ns + "content")})
    return out


def collect_feeds(cfg, db, today) -> None:
    for f in cfg.get("feeds", []):
        try:
            items = parse_feed(http_get(f["url"]))
        except Exception as e:
            log(f"Feed '{f['name']}' failed: {e}")
            continue
        added = 0
        for it in items[:cfg.get("items_per_feed", 25)]:
            link = it["link"].strip()
            if not link.startswith("https://") and not link.startswith("http://"):
                continue
            pub = parse_date(it["date"])
            cur = db.execute("INSERT OR IGNORE INTO articles VALUES(?,?,?,?,?,?,?)",
                             (link, strip_html(it["title"], 200), f["name"], f.get("kind", "blog"),
                              pub.isoformat() if pub else None, strip_html(it["summary"]), today))
            added += cur.rowcount
        if added:
            log(f"Feed '{f['name']}': {added} new items")
        time.sleep(0.5)
    db.commit()


def collect_intros(cfg, db, today) -> None:
    """Optional: first ~60s of transcript for today's top videos (needs youtube-transcript-api)."""
    n = cfg.get("intros_per_day", 0)
    if not n:
        return
    try:
        from youtube_transcript_api import YouTubeTranscriptApi  # type: ignore
    except ImportError:
        log("Intros skipped: youtube-transcript-api not installed (optional)")
        return
    rows = db.execute("""
        SELECT v.id FROM videos v JOIN video_stats s ON s.video_id=v.id AND s.day=?
        LEFT JOIN intros i ON i.video_id=v.id
        WHERE i.video_id IS NULL AND v.duration_s>180
        ORDER BY CAST(s.views AS REAL) / MAX((julianday('now') - julianday(v.published_at)), 0.5) DESC
        LIMIT ?""", (today, n)).fetchall()
    secs = cfg.get("intro_seconds", 60)
    for r in rows:
        try:
            api = YouTubeTranscriptApi()
            if hasattr(api, "fetch"):  # v1.x
                parts = [(s.start, s.text) for s in api.fetch(r["id"], languages=["en", "en-US", "en-GB"])]
            else:  # older versions
                parts = [(s["start"], s["text"]) for s in YouTubeTranscriptApi.get_transcript(r["id"], languages=["en"])]
            text = " ".join(x for start, x in parts if start < secs).replace("\n", " ")
            db.execute("INSERT OR REPLACE INTO intros VALUES(?,?,?)", (r["id"], text[:1500], today))
        except Exception as e:
            db.execute("INSERT OR REPLACE INTO intros VALUES(?,?,?)", (r["id"], "", today))
            log(f"No transcript for {r['id']}: {type(e).__name__}")
        time.sleep(1.5)
    db.commit()


def compute_keyword_stats(cfg, db, today) -> None:
    cutoff = (now_utc() - dt.timedelta(days=cfg["search_window_days"])).isoformat()
    rows = db.execute("""SELECT v.title, s.views FROM videos v JOIN video_stats s ON s.video_id=v.id AND s.day=?
                         WHERE v.published_at>=?""", (today, cutoff)).fetchall()
    vids, views = Counter(), Counter()
    for r in rows:
        for term in title_terms(r["title"] or ""):
            vids[term] += 1
            views[term] += r["views"] or 0
    db.execute("DELETE FROM keyword_stats WHERE day=?", (today,))
    db.executemany("INSERT INTO keyword_stats VALUES(?,?,?,?)",
                   [(today, t, c, views[t]) for t, c in vids.items() if c >= 2])
    db.commit()


# --------------------------------------------------------------------------- payload
def build_payload(cfg, db, today, quota_used) -> dict:
    now = now_utc()
    max_age = cfg["dashboard_max_age_days"]

    prev = {r["video_id"]: (r["views"], r["day"]) for r in db.execute("""
        SELECT s.video_id, s.views, s.day FROM video_stats s
        JOIN (SELECT video_id, MAX(day) d FROM video_stats WHERE day<? GROUP BY video_id) m
          ON m.video_id=s.video_id AND m.d=s.day""", (today,))}
    radar = {r["video_id"]: r["n"] for r in db.execute(
        "SELECT video_id, COUNT(*) n FROM video_stats GROUP BY video_id")}
    intros = {r["video_id"]: r["text"] for r in db.execute("SELECT video_id, text FROM intros WHERE text<>''")}

    videos = []
    for r in db.execute("""
        SELECT v.*, s.views, s.likes, s.comments, c.title ctitle, c.handle, c.subs, c.video_count, c.view_count, c.tracked
        FROM videos v JOIN video_stats s ON s.video_id=v.id AND s.day=?
        LEFT JOIN channels c ON c.id=v.channel_id""", (today,)):
        pub = parse_date(r["published_at"])
        if not pub:
            continue
        age_h = max((now - pub).total_seconds() / 3600, 1)
        rate_h = max(age_h, 6)  # avoid absurd views/day for videos only minutes old
        if age_h / 24 > max_age:
            continue
        views = r["views"] or 0
        avg = (r["view_count"] / r["video_count"]) if r["video_count"] and r["view_count"] else None
        growth = None
        if r["id"] in prev:
            pv, pday = prev[r["id"]]
            gap = max((dt.date.fromisoformat(today) - dt.date.fromisoformat(pday)).days, 1)
            growth = round((views - (pv or 0)) / gap)
        desc_lines = [l.strip() for l in (r["description"] or "").splitlines() if l.strip()]
        videos.append({
            "id": r["id"], "title": r["title"], "thumb": r["thumb"], "url": f"https://www.youtube.com/watch?v={r['id']}",
            "channel": r["ctitle"] or "", "channel_id": r["channel_id"],
            "channel_url": f"https://www.youtube.com/channel/{r['channel_id']}",
            "subs": r["subs"], "tier": tier_of(r["subs"]), "tracked": bool(r["tracked"]),
            "views": views, "likes": r["likes"], "comments": r["comments"],
            "published": pub.isoformat(), "age_days": round(age_h / 24, 1),
            "vpd": round(views / rate_h * 24), "outlier": round(views / avg, 2) if avg else None,
            "growth": growth, "radar_days": radar.get(r["id"], 1),
            "first_seen": r["first_seen"], "is_new": r["first_seen"] == today,
            "duration": r["duration_s"], "sources": json.loads(r["sources"] or "[]"),
            "hook": " ".join(desc_lines[:2])[:300], "intro": intros.get(r["id"], ""),
            "engagement": round((r["likes"] + r["comments"]) / views * 100, 2) if views else None,
        })
    videos.sort(key=lambda v: v["vpd"], reverse=True)
    videos = videos[:cfg["dashboard_max_videos"]]

    # Channels
    week_ago = (dt.date.fromisoformat(today) - dt.timedelta(days=7)).isoformat()
    old_subs = {r["channel_id"]: r["subs"] for r in db.execute("""
        SELECT s.channel_id, s.subs FROM channel_stats s
        JOIN (SELECT channel_id, MIN(day) d FROM channel_stats WHERE day>=? GROUP BY channel_id) m
          ON m.channel_id=s.channel_id AND m.d=s.day""", (week_ago,))}
    per_channel = defaultdict(list)
    for v in videos:
        per_channel[v["channel_id"]].append(v)
    channels = []
    for r in db.execute("SELECT * FROM channels WHERE last_seen>=? OR tracked=1", (week_ago,)):
        vs = per_channel.get(r["id"], [])
        if not vs and not r["tracked"]:
            continue
        outs = [v["outlier"] for v in vs if v["outlier"]]
        channels.append({
            "id": r["id"], "title": r["title"] or r["id"], "handle": r["handle"], "thumb": r["thumb"],
            "url": f"https://www.youtube.com/channel/{r['id']}", "subs": r["subs"], "tier": tier_of(r["subs"]),
            "subs_7d": (r["subs"] - old_subs[r["id"]]) if r["subs"] is not None and old_subs.get(r["id"]) is not None else None,
            "avg_views": round(r["view_count"] / r["video_count"]) if r["video_count"] else None,
            "on_radar": len(vs), "best_outlier": max(outs) if outs else None,
            "tracked": bool(r["tracked"]), "auto": bool(r["auto"]), "first_seen": r["first_seen"],
        })
    channels.sort(key=lambda c: (c["best_outlier"] or 0), reverse=True)

    # Rising keywords: today vs the average of the previous 7 snapshots
    prev_days = [r["day"] for r in db.execute(
        "SELECT DISTINCT day FROM keyword_stats WHERE day<? ORDER BY day DESC LIMIT 7", (today,))]
    hist = defaultdict(list)
    if prev_days:
        q = f"SELECT term, videos FROM keyword_stats WHERE day IN ({','.join('?' * len(prev_days))})"
        for r in db.execute(q, prev_days):
            hist[r["term"]].append(r["videos"])
    ever = {r["term"] for r in db.execute("SELECT DISTINCT term FROM keyword_stats WHERE day<?", (today,))}
    keywords = []
    for r in db.execute("SELECT term, videos, views FROM keyword_stats WHERE day=? AND videos>=3", (today,)):
        base = sum(hist[r["term"]]) / len(prev_days) if prev_days else 0
        keywords.append({"term": r["term"], "videos": r["videos"], "views": r["views"],
                         "delta": round(r["videos"] - base, 1), "is_new": bool(prev_days) and r["term"] not in ever})
    keywords.sort(key=lambda k: (k["delta"], k["views"]), reverse=True)
    keywords = keywords[:200]

    # Autocomplete suggestions, flag ones not seen before today
    seen_before = {(r["seed"], r["suggestion"]) for r in db.execute(
        "SELECT seed, suggestion FROM suggestions WHERE day<?", (today,))}
    had_history = bool(seen_before)
    sugg = defaultdict(list)
    for r in db.execute("SELECT seed, suggestion FROM suggestions WHERE day=?", (today,)):
        sugg[r["seed"]].append({"text": r["suggestion"],
                                "is_new": had_history and (r["seed"], r["suggestion"]) not in seen_before})

    # Articles
    art_cut = (now - dt.timedelta(days=cfg["article_max_age_days"])).isoformat()
    first_cut = (dt.date.fromisoformat(today) - dt.timedelta(days=cfg["article_max_age_days"])).isoformat()
    articles = []
    for r in db.execute("""SELECT * FROM articles WHERE (published_at IS NOT NULL AND published_at>=?)
                           OR (published_at IS NULL AND first_seen>=?)""", (art_cut, first_cut)):
        pub = parse_date(r["published_at"])
        articles.append({"url": r["url"], "title": r["title"], "source": r["source"], "kind": r["kind"],
                         "summary": r["summary"], "published": pub.isoformat() if pub else None,
                         "age_days": round((now - pub).total_seconds() / 86400, 1) if pub else None,
                         "is_new": r["first_seen"] == today})
    articles.sort(key=lambda a: a["published"] or "", reverse=True)

    return {
        "generated": now.isoformat(), "today": today, "quota_used": quota_used,
        "quota_budget": cfg["quota_budget"], "max_age": max_age,
        "videos": videos, "channels": channels, "keywords": keywords,
        "suggestions": dict(sugg), "articles": articles, "log": LOG[-80:],
        "snapshots": db.execute("SELECT COUNT(DISTINCT day) FROM video_stats").fetchone()[0],
    }


def render(payload: dict, out: Path) -> None:
    template = (ROOT / "dashboard_template.html").read_text(encoding="utf-8")
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    out.write_text(template.replace("__DATA__", data), encoding="utf-8")


# --------------------------------------------------------------------------- demo
def demo_payload() -> dict:
    import random
    random.seed(7)
    now = now_utc()
    titles = ["I tried monk mode for 30 days (honest results)", "How to actually stop procrastinating",
              "The 5am routine that changed my life", "Stoicism: 7 lessons for hard times",
              "Why discipline beats motivation every time", "Dopamine detox: what nobody tells you",
              "How I read 50 books a year", "Lock in: the 90-day reset", "Overthinking is ruining your life",
              "The habit that fixed my focus", "How to be more confident in 10 minutes",
              "Reinvent yourself in 2026", "Journaling prompts that actually work", "Deep work for students"]
    chans = [("Sample Small Channel", 42_000), ("Sample Mid Channel", 380_000),
             ("Sample Large Channel", 4_100_000), ("Sample Nano Channel", 6_200)]

    def thumb(i):
        hue = (i * 47) % 360
        svg = (f"<svg xmlns='http://www.w3.org/2000/svg' width='320' height='180'><rect width='320' height='180' "
               f"fill='hsl({hue},45%,55%)'/><text x='160' y='100' font-size='22' text-anchor='middle' "
               f"fill='white' font-family='sans-serif'>thumbnail {i + 1}</text></svg>")
        return "data:image/svg+xml;charset=utf-8," + urllib.parse.quote(svg)

    videos = []
    for i, t in enumerate(titles):
        cname, subs = chans[i % len(chans)]
        age = round(random.uniform(0.3, 14), 1)
        avg = subs * random.uniform(0.05, 0.3)
        views = int(avg * random.uniform(0.4, 9))
        videos.append({"id": f"demo{i}", "title": t, "thumb": thumb(i), "url": "https://www.youtube.com/",
                       "channel": cname, "channel_id": cname, "channel_url": "https://www.youtube.com/",
                       "subs": subs, "tier": tier_of(subs), "tracked": i % 3 == 0, "views": views,
                       "likes": views // 25, "comments": views // 300,
                       "published": (now - dt.timedelta(days=age)).isoformat(), "age_days": age,
                       "vpd": round(views / age), "outlier": round(views / avg, 2),
                       "growth": int(views * random.uniform(0.02, 0.3)), "radar_days": random.randint(1, 6),
                       "first_seen": "", "is_new": i % 4 == 1, "duration": random.choice([45, 610, 1240, 95, 2100]),
                       "sources": [f"kw:{random.choice(['discipline', 'stoicism', 'habits', 'productivity'])}"],
                       "hook": "Sample description hook from the first lines of the video description.",
                       "intro": "Sample intro transcript: what you say in the first 60 seconds appears here." if i % 2 else "",
                       "engagement": 4.1})
    videos.sort(key=lambda v: v["vpd"], reverse=True)
    channels = [{"id": c, "title": c, "handle": "@sample", "thumb": "", "url": "https://www.youtube.com/",
                 "subs": s, "tier": tier_of(s), "subs_7d": int(s * 0.01), "avg_views": int(s * 0.15),
                 "on_radar": 3, "best_outlier": 4.2, "tracked": True, "auto": c.startswith("Sample Nano"),
                 "first_seen": ""} for c, s in chans]
    kws = [("monk mode", 14, 3.2e6, 6.5, True), ("discipline", 40, 9.1e6, 4.0, False),
           ("dopamine", 18, 2.2e6, 2.1, False), ("lock in", 9, 1.4e6, 5.0, True), ("stoicism", 22, 3.0e6, -1.0, False)]
    keywords = [{"term": t, "videos": v, "views": int(w), "delta": d, "is_new": n} for t, v, w, d, n in kws]
    articles = [{"url": "https://example.com/", "title": f"Sample {k} item {i}", "source": f"Sample {k} source",
                 "kind": k, "summary": "Short summary of the post appears here.",
                 "published": (now - dt.timedelta(days=i)).isoformat(), "age_days": i, "is_new": i == 0}
                for k in ("blog", "newsletter", "reddit", "news") for i in range(3)]
    return {"generated": now.isoformat(), "today": now.date().isoformat(), "quota_used": 4321, "quota_budget": 9000,
            "max_age": 30, "videos": videos, "channels": channels, "keywords": keywords,
            "suggestions": {"self improvement": [{"text": "self improvement tips for men", "is_new": True},
                                                 {"text": "self improvement routine", "is_new": False}]},
            "articles": articles, "log": ["Demo data: run without --demo to fetch real data."], "snapshots": 1}


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description="Self-improvement YouTube trend radar")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--demo", action="store_true", help="Build the dashboard with sample data (no API key needed)")
    ap.add_argument("--skip-youtube", action="store_true", help="Only refresh feeds and suggestions")
    ap.add_argument("--rebuild", action="store_true", help="Rebuild the dashboard from stored data only")
    args = ap.parse_args()

    if args.demo:
        render(demo_payload(), ROOT / "dashboard_demo.html")
        print(f"Wrote {ROOT / 'dashboard_demo.html'}")
        return 0

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    db = open_db(ROOT / cfg.get("db_path", "data/tracker.db"))
    today = dt.date.today().isoformat()
    quota_used = 0

    if not args.rebuild:
        if not args.skip_youtube:
            key = os.environ.get("YT_API_KEY", "").strip()
            if not key:
                print("Set the YT_API_KEY environment variable (see README).", file=sys.stderr)
                return 2
            yt = YouTube(key, cfg["quota_budget"])
            try:
                collect_youtube(cfg, db, yt, today)
            except QuotaBudgetExceeded as e:
                log(f"Quota budget reached at {e}")
            except RuntimeError as e:
                log(str(e))
            quota_used = yt.used
            log(f"YouTube quota used: {yt.used} units")
            auto_track(cfg, db, today)
            compute_keyword_stats(cfg, db, today)
            collect_intros(cfg, db, today)
        collect_suggestions(cfg, db, today)
        collect_feeds(cfg, db, today)

    payload = build_payload(cfg, db, today, quota_used)
    out = ROOT / "dashboard.html"
    render(payload, out)
    archive = ROOT / "data" / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(out, archive / f"{today}.html")
    log(f"Dashboard written: {out} ({len(payload['videos'])} videos)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
