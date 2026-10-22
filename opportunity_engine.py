"""
Weekly Long-Form Opportunity Engine
-----------------------------------
Runs once a week (Sunday by default) inside tracker.py, after the daily collection.

It turns the collected YouTube data into a ranked list of long-form video opportunities for your
channel, following a fixed brief:
  * fixed 13 content pillars and keyword map (config.json -> clusters)
  * strict long-form eligibility (5+ min, English, last 100 days, no Shorts/podcasts/music/etc.)
  * channel baseline: Channel Performance Multiple = views / median of previous 10 comparable videos
  * outlier qualification (3+ conditions, with at least one real performance signal)
  * fixed 100-point Opportunity Score + penalties + Produce Now gate
  * trend validation across unrelated channels
  * week-to-week status (New, Rising, Holding, Falling, Dropped, Recovered) with permanent snapshots
  * AI deep analysis + original video recommendation for the Top 10
  * weekly report saved to data/reports/ and emailed (Best 5 only)

Every number carries an evidence label: Verified (straight from the YouTube API), Calculated
(derived from verified data), Inferred (AI or rule-based judgement), Unavailable.
vidIQ scores are not available through any API, so they are always marked Unavailable.
"""
import base64
import datetime as dt
import html
import json
import os
import re
import smtplib
import ssl
import statistics
import time
from collections import defaultdict
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

H = None  # helpers passed in from tracker.py (log, http_get, http_post_json, parse_date, now_utc, ...)
ROOT = Path(__file__).resolve().parent

PILLARS = ["Productivity", "Organization", "Planning", "Goals and Goal Achievement", "Time Management",
           "Focus and Deep Work", "Discipline and Consistency", "Habits and Routines",
           "Procrastination and Execution", "Life Reset", "Digital Productivity",
           "Personal Productivity Systems", "Practical Personal Growth"]
SHARE_DRIVERS = ["relatable identity", "quotable framework or line", "surprising stat or reframe",
                 "before/after transformation", "checklist or reference", "none"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS opportunities(
  video_id TEXT PRIMARY KEY, data TEXT, status TEXT, rank INTEGER, score REAL, decision TEXT,
  first_discovered TEXT, last_checked TEXT);
CREATE TABLE IF NOT EXISTS opportunity_snapshots(
  video_id TEXT, snapshot_date TEXT, data TEXT, PRIMARY KEY(video_id, snapshot_date));
CREATE TABLE IF NOT EXISTS channel_baselines(channel_id TEXT, day TEXT, data TEXT, PRIMARY KEY(channel_id, day));
CREATE TABLE IF NOT EXISTS weekly_runs(day TEXT PRIMARY KEY, summary TEXT);
CREATE TABLE IF NOT EXISTS my_channel(id TEXT PRIMARY KEY, data TEXT, day TEXT);
CREATE TABLE IF NOT EXISTS search_log(day TEXT, unit TEXT, ids TEXT);
"""


def ensure_schema(db):
    db.executescript(SCHEMA)
    cols = {r[1] for r in db.execute("PRAGMA table_info(videos)")}
    for col, typ in (("category_id", "TEXT"), ("lang", "TEXT"), ("live", "INTEGER"), ("kids", "INTEGER")):
        if col not in cols:
            db.execute(f"ALTER TABLE videos ADD COLUMN {col} {typ}")
    db.commit()


# --------------------------------------------------------------------------- classification helpers
def size_class(subs):
    if subs is None:
        return "Hidden"
    if subs < 10_000:
        return "Micro"
    if subs < 100_000:
        return "Small"
    if subs < 500_000:
        return "Medium"
    if subs <= 2_000_000:
        return "Large"
    return "Major"


def viral_stage(age_days):
    if age_days < 1:
        return None
    if age_days <= 7:
        return "Immediate Signal"
    if age_days <= 21:
        return "Emerging Viral"
    if age_days <= 60:
        return "Validated Viral"
    if age_days <= 100:
        return "Extended Validation"
    return None


def cpm_class(cpm):
    if cpm is None:
        return "Unavailable"
    if cpm < 1:
        return "Underperforming"
    if cpm < 2:
        return "Normal"
    if cpm < 4:
        return "Strong"
    if cpm < 8:
        return "Breakout"
    return "Exceptional Outlier"


def decision_for(score):
    if score >= 85:
        return "Produce Now"
    if score >= 75:
        return "High-Confidence Opportunity"
    if score >= 65:
        return "Strong Opportunity"
    if score >= 50:
        return "Monitor or Test"
    return "Skip"


def non_latin_ratio(text):
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0
    return sum(1 for c in letters if ord(c) > 0x24F) / len(letters)


def cluster_regexes(cfg):
    out = []
    for cl in cfg["clusters"]:
        kws = sorted({k.lower().strip() for k in cl["keywords"] if k.strip()}, key=len, reverse=True)
        pattern = r"(?<![a-z0-9])(?:" + "|".join(re.escape(k) for k in kws) + r")(?![a-z0-9])"
        out.append((cl["name"], re.compile(pattern)))
    return out


def classify_topic(title, tags, desc, regexes):
    """Score each pillar by keyword hits (title x3, tags x2, description x1)."""
    t, g, d = title.lower(), " | ".join(tags).lower(), desc[:600].lower()
    best = None
    for name, rx in regexes:
        th, gh, dh = set(rx.findall(t)), set(rx.findall(g)), set(rx.findall(d))
        score = 3 * len(th) + 2 * len(gh) + len(dh)
        if score and (best is None or score > best[1]):
            primary = max(th or gh or dh, key=len)
            secondary = sorted((th | gh | dh) - {primary}, key=len, reverse=True)[:5]
            best = (name, score, len(th), primary, secondary)
    return best  # (pillar, score, title_hits, primary_keyword, secondary_keywords) or None


def eligibility(v, cfg, my_id):
    e = cfg["eligibility"]
    title = v["title"] or ""
    tl = title.lower()
    if my_id and v["channel_id"] == my_id:
        return False, "your own channel"
    if (v["duration_s"] or 0) < e["min_seconds"]:
        return False, "Short or under 5 minutes"
    if v["live"]:
        return False, "livestream replay"
    if v["kids"]:
        return False, "made for kids"
    if (v["category_id"] or "") in e["excluded_categories"]:
        return False, "excluded category (music, news, sports, gaming)"
    lang = (v["lang"] or "").lower()
    if lang and not lang.startswith("en"):
        return False, "not English"
    if not lang and non_latin_ratio(title) > 0.3:
        return False, "not English"
    for pat in e["title_exclude"]:
        if re.search(pat, tl):
            return False, f"excluded format ({pat})"
    dl = (v["description"] or "")[:400].lower()
    for pat in e["description_exclude"]:
        if re.search(pat, dl):
            return False, f"excluded format ({pat})"
    if any(re.search(p, tl) for p in e["soft_exclude"]) and not any(re.search(p, tl) for p in e["soft_allow"]):
        return False, "home/desk/hardware content without a productivity system"
    return True, ""


# --------------------------------------------------------------------------- scoring (fixed weights)
def band(value, lo, hi, position):
    """position 0..1 inside an inclusive band"""
    return round(lo + (hi - lo) * max(0.0, min(1.0, position)))


def vph_points(pct):
    if pct is None:
        return 0
    if pct >= 0.90:
        return band(None, 22, 25, (pct - 0.90) / 0.10)
    if pct >= 0.75:
        return band(None, 17, 21, (pct - 0.75) / 0.15)
    if pct >= 0.50:
        return band(None, 12, 16, (pct - 0.50) / 0.25)
    if pct >= 0.25:
        return band(None, 6, 11, (pct - 0.25) / 0.25)
    return band(None, 0, 5, pct / 0.25)


def outlier_points(cpm, eng_rel):
    pos = 0.5
    if eng_rel is None or eng_rel <= 0.8:
        pos = 0.0
    elif eng_rel >= 1.2:
        pos = 1.0
    if cpm is None:
        return band(None, 0, 4, pos)
    if cpm >= 8:
        return band(None, 18, 20, pos)
    if cpm >= 4:
        return band(None, 14, 17, pos)
    if cpm >= 2:
        return band(None, 10, 13, pos)
    if cpm >= 1:
        return band(None, 5, 9, pos)
    return band(None, 0, 4, pos)


def vs_points(ratio):
    if ratio is None:
        return 0
    if ratio > 3.0:
        return 15
    if ratio >= 2.0:
        return 13
    if ratio >= 1.0:
        return 11
    if ratio >= 0.5:
        return 8
    if ratio >= 0.25:
        return 5
    return round(ratio / 0.25 * 3)


def recency_points(stage, momentum):
    if stage in ("Immediate Signal", "Emerging Viral"):
        return 5
    if momentum == "declining":
        return 1
    if stage == "Validated Viral":
        return 4 if momentum == "growing" else 3
    if stage == "Extended Validation":
        return 3 if momentum in ("growing", "sustained") else 2
    return 0


PENALTY_CAPS = {"fame": 15, "saturation": 10, "production": 10, "data_incomplete": 10, "copying": 10,
                "loose_alignment": 10, "momentum_decline": 10, "guest_event": 10}


# --------------------------------------------------------------------------- data gathering
def my_channel_info(cfg, db, yt, today):
    """Your own channel: stats + recent long-form videos, used for fit and realism judgements."""
    mc = cfg.get("my_channel", {})
    cid = mc.get("channel_id")
    if not cid:
        return None
    row = db.execute("SELECT data FROM my_channel WHERE id=? AND day=?", (cid, today)).fetchone()
    if row:
        return json.loads(row["data"])
    info = {"id": cid, "name": mc.get("name", ""), "about": mc.get("about", "")}
    try:
        d = yt.call("channels", part="snippet,statistics,contentDetails", id=cid)
        it = (d.get("items") or [{}])[0]
        st = it.get("statistics", {})
        info["subs"] = None if st.get("hiddenSubscriberCount") else int(st.get("subscriberCount", 0))
        info["title"] = it.get("snippet", {}).get("title", info["name"])
        uploads = it.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads") or "UU" + cid[2:]
        pl = yt.call("playlistItems", part="contentDetails", playlistId=uploads, maxResults=50)
        ids = [x["contentDetails"]["videoId"] for x in pl.get("items", [])]
        vids = []
        if ids:
            vd = yt.call("videos", part="snippet,statistics,contentDetails", id=",".join(ids), maxResults=50)
            for v in vd.get("items", []):
                dur = H.iso_duration(v.get("contentDetails", {}).get("duration"))
                vids.append({"title": v["snippet"].get("title", ""), "views": int(v.get("statistics", {}).get("viewCount", 0)),
                             "duration_s": dur, "published": v["snippet"].get("publishedAt", "")[:10]})
        longform = [v for v in vids if v["duration_s"] >= 300]
        info["recent_longform"] = longform[:20]
        info["median_longform_views"] = statistics.median([v["views"] for v in longform[:10]]) if len(longform) >= 3 else None
    except H.QuotaBudgetExceeded:
        raise
    except Exception as e:
        H.log(f"Could not load your channel: {e}")
    db.execute("INSERT OR REPLACE INTO my_channel VALUES(?,?,?)", (cid, json.dumps(info), today))
    db.commit()
    return info


def channel_baseline(db, yt, cid, uploads, today):
    row = db.execute("SELECT data FROM channel_baselines WHERE channel_id=? AND day=?", (cid, today)).fetchone()
    if row:
        return json.loads(row["data"])
    pl = yt.call("playlistItems", part="contentDetails", playlistId=uploads or "UU" + cid[2:], maxResults=50)
    ids = [x["contentDetails"]["videoId"] for x in pl.get("items", []) if x.get("contentDetails", {}).get("videoId")]
    vids = []
    if ids:
        vd = yt.call("videos", part="snippet,statistics,contentDetails,liveStreamingDetails", id=",".join(ids), maxResults=50)
        for v in vd.get("items", []):
            st = v.get("statistics", {})
            vids.append({"id": v["id"], "published": v["snippet"].get("publishedAt", ""),
                         "duration_s": H.iso_duration(v.get("contentDetails", {}).get("duration")),
                         "live": "liveStreamingDetails" in v, "views": int(st.get("viewCount", 0)),
                         "likes": int(st.get("likeCount", 0)), "comments": int(st.get("commentCount", 0))})
    db.execute("INSERT OR REPLACE INTO channel_baselines VALUES(?,?,?)", (cid, today, json.dumps(vids)))
    return vids


def baseline_for(video, chan_vids, now):
    """Median of the channel's previous 10 comparable long-form videos (at least 5 required)."""
    pub = video["published"]
    comp = []
    for x in chan_vids:
        if x["id"] == video["video_id"] or x["live"] or x["duration_s"] < 300:
            continue
        xp = H.parse_date(x["published"])
        if not xp or xp >= pub or (now - xp).days < 7:
            continue
        comp.append((xp, x))
    comp.sort(key=lambda t: t[0], reverse=True)
    comp = [x for _, x in comp[:10]]
    if len(comp) < 5:
        return None
    med_views = statistics.median([x["views"] for x in comp])
    engs = [(x["likes"] + x["comments"]) / x["views"] for x in comp if x["views"]]
    crs = [x["comments"] / x["views"] * 1000 for x in comp if x["views"]]
    return {"median_views": med_views, "n": len(comp),
            "median_eng": statistics.median(engs) if engs else None,
            "median_comment_rate": statistics.median(crs) if crs else None}


def momentum_for(db, vid, views_now, today, lifetime_vph):
    """VPH over the last ~7 days and the ~7 days before that, from daily snapshots."""
    rows = db.execute("SELECT day, views FROM video_stats WHERE video_id=? AND day<? ORDER BY day DESC", (vid, today)).fetchall()
    t = dt.date.fromisoformat(today)

    def nearest(days_back):
        target = t - dt.timedelta(days=days_back)
        best = None
        for r in rows:
            d = dt.date.fromisoformat(r["day"])
            if d <= target + dt.timedelta(days=1) and (best is None or abs((d - target).days) < abs((dt.date.fromisoformat(best["day"]) - target).days)):
                best = r
        return best

    a, b = nearest(7), nearest(14)
    out = {"recent_vph": None, "prev_vph": None, "label": "unknown"}
    if a:
        hrs = max((t - dt.date.fromisoformat(a["day"])).days * 24, 24)
        out["recent_vph"] = round((views_now - a["views"]) / hrs, 1)
        if b and b["day"] < a["day"]:
            hrs2 = max((dt.date.fromisoformat(a["day"]) - dt.date.fromisoformat(b["day"])).days * 24, 24)
            out["prev_vph"] = round((a["views"] - b["views"]) / hrs2, 1)
    r, p = out["recent_vph"], out["prev_vph"]
    if r is not None and p:
        out["label"] = "growing" if r >= p * 1.1 else "declining" if r < p * 0.7 else "sustained"
    elif r is not None and lifetime_vph:
        out["label"] = "sustained" if r >= lifetime_vph * 0.5 else "declining" if r < lifetime_vph * 0.2 else "unknown"
    return out


# --------------------------------------------------------------------------- AI
def ai_available():
    return bool(os.environ.get("GEMINI_API_KEY", "").strip() or os.environ.get("ANTHROPIC_API_KEY", "").strip())


def fetch_image_b64(url):
    try:
        return base64.b64encode(H.http_get(url, retries=2, timeout=20)).decode("ascii")
    except Exception:
        return None


def ai_json(cfg, prompt, images=()):
    """Call Gemini (preferred, free tier) or Anthropic and parse a JSON answer. images: [(label, b64jpeg)]"""
    gem = os.environ.get("GEMINI_API_KEY", "").strip()
    ant = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    time.sleep(cfg.get("ai_call_delay_seconds", 7))  # stay inside free-tier rate limits
    if gem:
        model = cfg.get("gemini_model", "gemini-2.5-flash")
        parts = [{"text": prompt}]
        for label, b64 in images:
            parts += [{"text": label}, {"inlineData": {"mimeType": "image/jpeg", "data": b64}}]
        d = H.http_post_json(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                             {"contents": [{"parts": parts}],
                              "generationConfig": {"responseMimeType": "application/json", "temperature": 0.3}},
                             {"x-goog-api-key": gem}, timeout=240)
        text = "".join(p.get("text", "") for p in d.get("candidates", [{}])[0].get("content", {}).get("parts", []))
    elif ant:
        model = cfg.get("anthropic_model", "claude-haiku-5-5")
        content = []
        for label, b64 in images:
            content += [{"type": "text", "text": label},
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}}]
        content.append({"type": "text", "text": prompt + "\n\nReturn only the JSON object, with no other text."})
        d = H.http_post_json("https://api.anthropic.com/v1/messages",
                             {"model": model, "max_tokens": 8000, "messages": [{"role": "user", "content": content}]},
                             {"x-api-key": ant, "anthropic-version": "2023-06-01"}, timeout=240)
        text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
    else:
        raise RuntimeError("no AI key")
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    return json.loads(text)


def channel_context(cfg, mine):
    mc = cfg.get("my_channel", {})
    lines = [f"Channel: {mc.get('name', '')} ({mc.get('handle', '')})",
             f"Positioning: {mc.get('positioning', '')}",
             "Fixed content pillars: " + "; ".join(PILLARS)]
    if mine:
        lines.append(f"Subscribers: {mine.get('subs', 'unknown')}; median views of recent long-form videos: {mine.get('median_longform_views', 'unknown')}")
        recent = mine.get("recent_longform", [])[:15]
        if recent:
            lines.append("Recent long-form uploads (title | views):")
            lines += [f"- {v['title']} | {v['views']}" for v in recent]
    return "\n".join(lines)


SCORING_RUBRIC = """Score each video for THIS creator using these fixed rubrics:
- alignment (0-15): fit with the creator's fixed pillars and practical, actionable positioning. 13-15 perfect, 10-12 strong, 6-9 moderate, 1-5 weak, 0 not aligned.
- packaging (0-10): title and thumbnail judged together. 9-10 exceptional, 7-8 strong, 5-6 good, 3-4 average, 0-2 weak. If no clear shareability driver exists, packaging may not exceed 6.
- replicability (0-10): could this creator realistically make an ORIGINAL version? 9-10 highly replicable, 7-8 with clear differentiation, 4-6 moderately difficult, 1-3 difficult, 0 not realistic.
- shareability_driver: exactly one of: relatable identity | quotable framework or line | surprising stat or reframe | before/after transformation | checklist or reference | none
- penalties (only when evidence supports them): fame 0-15 (success depends on creator fame/loyalty), saturation 0-10, production 0-10 (unrealistic production), copying 0-10 (adaptation risks copying), loose_alignment 0-10, guest_event 0-10 (depends on a guest, news event or unique access).
- exclude=true only if the video is actually a podcast, podcast clip, interview, music/timer, speech or quote compilation, reaction, reupload, news/politics, sermon, kids content, English lesson, home-storage/interior or hardware/desk video without a productivity system, or relies on a non-replicable personal event.
Never invent numbers you were not given."""


def ai_batch_score(cfg, batch, mine):
    items, images = [], []
    for o in batch:
        items.append({"id": o["video_id"], "title": o["title"], "channel": o["channel"], "channel_size": o["size"],
                      "subscribers": o["subs"], "duration_min": round(o["duration_s"] / 60, 1), "age_days": o["age_days"],
                      "views": o["views"], "vph": o["vph"], "views_to_subs": o["vs_ratio"],
                      "channel_performance_multiple": o["cpm"], "engagement_rate_pct": o["eng_rate_pct"],
                      "matched_pillar": o["cluster"], "description_start": o["description"][:300], "tags": o["tags"][:10]})
        b64 = fetch_image_b64(f"https://i.ytimg.com/vi/{o['video_id']}/mqdefault.jpg")
        if b64:
            images.append((f"Thumbnail for video id {o['video_id']}:", b64))
    prompt = f"""You are a research analyst for a YouTube creator.
CREATOR CONTEXT
{channel_context(cfg, mine)}

{SCORING_RUBRIC}

Pillar must be exactly one of: {", ".join(PILLARS)}.
topic_key: a short lowercase canonical topic (2-5 words) so that videos about the same underlying idea share the same key (e.g. "weekly reset routine", "time blocking system").

VIDEOS (JSON):
{json.dumps(items, ensure_ascii=False)}

Thumbnails follow, each labelled with its video id.

Return JSON: {{"videos": [{{"id": str, "pillar": str, "primary_keyword": str, "secondary_keywords": [str],
"topic_key": str, "viewer_problem": str, "transformation": str, "alignment": int, "packaging": int,
"replicability": int, "shareability_driver": str, "production_difficulty": "Low|Medium|High",
"production_cost": "Low|Medium|High", "production_hours": number, "clear_problem": bool, "adaptable": bool,
"original_angle_exists": bool, "evergreen": bool, "exclude": bool, "exclude_reason": str,
"penalties": {{"fame": int, "saturation": int, "production": int, "copying": int, "loose_alignment": int, "guest_event": int}}}}]}}"""
    data = ai_json(cfg, prompt, images)
    return {str(v.get("id")): v for v in data.get("videos", [])}


DEEP_SCHEMA = """{
 "why_working": {"audience_problem": str, "desired_transformation": str, "emotional_trigger": str, "why_clicking": str,
   "why_timely": str, "why_unusual": str, "main_driver": str, "success_type": "topic-led|packaging-led|creator-led|trend-led",
   "still_active": str, "replicable": str},
 "title_analysis": {"formula": str, "promise": str, "curiosity_gap": str, "specificity": str, "emotional_trigger": str,
   "transformation_angle": str, "identity_angle": str, "contrarian_angle": str, "why_it_works": str, "adapt": str, "do_not_copy": str},
 "thumbnail_analysis": {"visual_concept": str, "main_subject": str, "thumbnail_text": str, "contrast": str, "curiosity_gap": str,
   "emotional_trigger": str, "mobile_readability": str, "title_relationship": str, "why_clicks": str, "adapt": str, "do_not_copy": str},
 "hook_analysis": {"basis": "transcript|inferred", "opening_line": str, "first_30s": str, "promise": str, "stakes": str,
   "curiosity_loop": str, "credibility": str, "pattern_interrupt": str, "retention_mechanism": str},
 "engagement": {"read": "high|normal|low|unavailable", "basis": str, "comment_pattern": str, "shareability_driver": str, "shareability_risk": str},
 "replication": {"micro": str, "small": str, "medium": str, "needs_authority": str, "needs_expensive_production": str,
   "needs_guest_or_access": str, "talking_head": str, "faceless": str, "what_must_change": str, "difficulty": "Low|Medium|High"},
 "packaging_score": int,
 "recommendation": {"concept": str, "why_worth_it": str, "target_viewer": str, "viewer_problem": str, "transformation": str,
   "unique_angle": str, "differentiation": str, "title": str, "backup_title": str, "thumbnail_concept": str,
   "thumbnail_text": str, "hook": str, "first_30s": str, "sections": [str], "length": str, "format": str,
   "research": str, "visuals": str, "difficulty": "Low|Medium|High", "cost": "Low|Medium|High", "hours": number,
   "risk": str, "risk_reduction": str, "evidence": str, "confidence": "Low|Medium|High|Very High"},
 "performance_summary": str
}"""


def ai_deep(cfg, o, mine, transcript, comments):
    img = fetch_image_b64(f"https://i.ytimg.com/vi/{o['video_id']}/hqdefault.jpg")
    facts = {k: o.get(k) for k in ("title", "channel", "size", "subs", "published", "age_days", "duration_s", "views",
                                    "likes", "comments", "vph", "vs_ratio", "cpm", "cpm_class", "eng_rate_pct",
                                    "channel_eng_pct", "stage", "trend", "cluster", "primary_keyword", "topic_key")}
    prompt = f"""You are a research analyst for a top-1% YouTube creator. Deeply analyse one proven video and design an
ORIGINAL video for the creator below. Do not copy the source title, thumbnail layout, script, hook wording,
personal stories, unique frameworks, examples or visual identity: adapt the validated audience demand only.
Thumbnail text in your recommendation must be 4 words or fewer.

CREATOR CONTEXT
{channel_context(cfg, mine)}

SOURCE VIDEO DATA (Verified from the YouTube API or Calculated from it):
{json.dumps(facts, ensure_ascii=False, default=str)}
Description start: {o['description'][:600]}

FIRST ~60 SECONDS OF TRANSCRIPT: {transcript or "UNAVAILABLE - base the hook analysis on title, thumbnail and description, set basis to 'inferred' and say so."}

MOST-LIKED VISIBLE COMMENTS: {json.dumps(comments[:12], ensure_ascii=False) if comments else "UNAVAILABLE - say 'not enough visible comment data'."}

Production cost categories: Low = Rs 0-5,000, Medium = Rs 5,001-20,000, High = above Rs 20,000.
Hours include research, scripting, filming, editing, thumbnail and revisions.
If something cannot be determined from the data, write "Unavailable" rather than guessing.

Return JSON exactly in this shape:
{DEEP_SCHEMA}"""
    return ai_json(cfg, prompt, [("Source thumbnail:", img)] if img else [])


# --------------------------------------------------------------------------- comments / transcripts for deep analysis
def get_comments(db, yt, vid, today):
    rows = db.execute("SELECT text, likes FROM comments WHERE video_id=? AND text<>'' ORDER BY rank", (vid,)).fetchall()
    if rows:
        return [{"text": r["text"], "likes": r["likes"]} for r in rows]
    try:
        d = yt.call("commentThreads", part="snippet", videoId=vid, order="relevance", maxResults=50, textFormat="plainText")
    except Exception:
        return []
    items = []
    for it in d.get("items", []):
        sn = it.get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
        text = re.sub(r"\s+", " ", sn.get("textDisplay") or "").strip()
        if text:
            items.append((int(sn.get("likeCount", 0)), text[:400]))
    items.sort(reverse=True)
    for rank, (likes, text) in enumerate(items[:10], 1):
        db.execute("INSERT OR REPLACE INTO comments VALUES(?,?,?,?,?)", (vid, rank, text, likes, today))
    return [{"text": t, "likes": l} for l, t in items[:10]]


def get_transcript(db, vid, today):
    row = db.execute("SELECT text FROM intros WHERE video_id=?", (vid,)).fetchone()
    if row and row["text"]:
        return row["text"]
    try:
        from youtube_transcript_api import YouTubeTranscriptApi  # type: ignore
        api = YouTubeTranscriptApi()
        if hasattr(api, "fetch"):
            parts = [(s.start, s.text) for s in api.fetch(vid, languages=["en", "en-US", "en-GB"])]
        else:
            parts = [(s["start"], s["text"]) for s in YouTubeTranscriptApi.get_transcript(vid, languages=["en"])]
        text = " ".join(x for st, x in parts if st < 60).replace("\n", " ")[:1500]
        db.execute("INSERT OR REPLACE INTO intros VALUES(?,?,?)", (vid, text, today))
        return text
    except Exception:
        return None


# --------------------------------------------------------------------------- main weekly run
def weekly_due(cfg, db, today, force):
    if db.execute("SELECT 1 FROM weekly_runs WHERE day=?", (today,)).fetchone():
        return False
    return force or dt.date.fromisoformat(today).weekday() == cfg.get("weekly_weekday", 6)


def run_weekly(cfg, db, yt, today, helpers):
    global H
    H = helpers
    ensure_schema(db)
    now = H.now_utc()
    limitations = ["vidIQ breakout/outlier scores: Unavailable (vidIQ has no public API); Channel Performance Multiple is calculated instead.",
                   "Research is stored in this dashboard's database instead of Notion."]
    summary = {"day": today, "limitations": limitations}
    use_ai = ai_available()
    if not use_ai:
        limitations.append("No GEMINI_API_KEY or ANTHROPIC_API_KEY: alignment, packaging and replicability are rule-based estimates and deep analysis is skipped.")

    mine = None
    try:
        mine = my_channel_info(cfg, db, yt, today)
    except H.QuotaBudgetExceeded:
        limitations.append("YouTube quota ran out before your channel could be loaded.")
    my_id = cfg.get("my_channel", {}).get("channel_id")

    # ---- 1. candidate pool: videos with today's stats, eligible, 1-100 days old, inside the fixed niche
    regexes = cluster_regexes(cfg)
    rows = db.execute("""
        SELECT v.*, s.views, s.likes, s.comments, c.title ctitle, c.subs, c.uploads
        FROM videos v JOIN video_stats s ON s.video_id=v.id AND s.day=(SELECT MAX(day) FROM video_stats WHERE video_id=v.id)
        LEFT JOIN channels c ON c.id=v.channel_id""").fetchall()
    reviewed, removed_irrelevant, pool = 0, 0, []
    reasons = defaultdict(int)
    for r in rows:
        pub = H.parse_date(r["published_at"])
        if not pub:
            continue
        age = (now - pub).total_seconds() / 86400
        if age > 100 or age < 1:
            continue
        reviewed += 1
        ok, why = eligibility(r, cfg, my_id)
        if not ok:
            removed_irrelevant += 1
            reasons[why.split(" (")[0]] += 1
            continue
        tags = json.loads(r["tags"] or "[]")
        topic = classify_topic(r["title"] or "", tags, r["description"] or "", regexes)
        if not topic or (topic[2] == 0 and topic[1] < 3):
            removed_irrelevant += 1
            reasons["outside the 13 pillars"] += 1
            continue
        views, subs = r["views"] or 0, r["subs"]
        age_h = age * 24
        o = {"video_id": r["id"], "title": r["title"], "url": f"https://www.youtube.com/watch?v={r['id']}",
             "thumb": f"https://i.ytimg.com/vi/{r['id']}/hqdefault.jpg", "channel": r["ctitle"] or "",
             "channel_id": r["channel_id"], "channel_url": f"https://www.youtube.com/channel/{r['channel_id']}",
             "uploads": r["uploads"], "subs": subs, "size": size_class(subs), "published_dt": pub,
             "published": pub, "age_days": round(age, 1), "duration_s": r["duration_s"], "views": views,
             "likes": r["likes"], "comments": r["comments"], "vph": round(views / age_h, 1),
             "vs_ratio": round(views / subs, 2) if subs else None,
             "eng_rate": (r["likes"] + r["comments"]) / views if views else None,
             "comment_rate": r["comments"] / views * 1000 if views else None,
             "cluster": topic[0], "primary_keyword": topic[3], "secondary_keywords": topic[4],
             "stage": viral_stage(age), "description": r["description"] or "", "tags": tags}
        o["eng_rate_pct"] = round(o["eng_rate"] * 100, 2) if o["eng_rate"] is not None else None
        pool.append(o)
    summary.update(videos_reviewed=reviewed, irrelevant_removed=removed_irrelevant, removal_reasons=dict(reasons))
    H.log(f"Weekly: {reviewed} videos reviewed, {len(pool)} eligible long-form videos inside your pillars")

    # ---- 2. VPH percentile within the same viral stage
    by_stage = defaultdict(list)
    for o in pool:
        by_stage[o["stage"]].append(o["vph"])
    for o in pool:
        arr = sorted(by_stage[o["stage"]])
        o["vph_pct"] = (sum(1 for x in arr if x <= o["vph"]) - 1) / max(len(arr) - 1, 1) if len(arr) > 1 else 0.5
    pool_eng = [o["eng_rate"] for o in pool if o["eng_rate"] is not None]
    pool_eng_median = statistics.median(pool_eng) if pool_eng else None

    # ---- 3. pre-screen: needs at least one real performance signal before spending quota on baselines
    def perf_signal(o):
        return o["vph_pct"] >= 0.75 or (o["vs_ratio"] or 0) >= 0.5
    screened = [o for o in pool if perf_signal(o)]
    screened.sort(key=lambda o: (vph_points(o["vph_pct"]) + vs_points(o["vs_ratio"])), reverse=True)
    screened = screened[:cfg.get("max_candidates", 300)]

    # ---- 4. channel baselines (2 units per channel)
    chan_cache, quota_hit = {}, False
    for o in screened:
        cid = o["channel_id"]
        if cid not in chan_cache and not quota_hit and len(chan_cache) < cfg.get("baseline_max_channels", 400):
            try:
                chan_cache[cid] = channel_baseline(db, yt, cid, o["uploads"], today)
            except H.QuotaBudgetExceeded:
                quota_hit = True
                limitations.append("YouTube quota ran out while building channel baselines; some Channel Performance Multiples are Unavailable.")
            except Exception as e:
                chan_cache[cid] = None
                H.log(f"Baseline for {cid} failed: {str(e)[:80]}")
        base = baseline_for(o, chan_cache.get(cid) or [], now) if chan_cache.get(cid) else None
        o["baseline_n"] = base["n"] if base else 0
        o["cpm"] = round(o["views"] / base["median_views"], 2) if base and base["median_views"] else None
        o["channel_eng"] = base["median_eng"] if base else None
        o["channel_eng_pct"] = round(o["channel_eng"] * 100, 2) if o["channel_eng"] is not None else None
        o["channel_comment_rate"] = base["median_comment_rate"] if base else None
        o["eng_rel"] = (o["eng_rate"] / o["channel_eng"]) if o["eng_rate"] is not None and o["channel_eng"] else None
        o["cpm_class"] = cpm_class(o["cpm"])
        o["momentum"] = momentum_for(db, o["video_id"], o["views"], today, o["vph"])
    db.commit()

    # ---- 5. AI scoring (or rule-based fallback)
    for o in screened:
        o["ai"] = None
    if use_ai:
        bs = cfg.get("ai_batch_size", 20)
        for i in range(0, min(len(screened), cfg.get("ai_max_candidates", 160)), bs):
            batch = screened[i:i + bs]
            try:
                res = ai_batch_score(cfg, batch, mine)
                for o in batch:
                    o["ai"] = res.get(o["video_id"])
            except Exception as e:
                H.log(f"AI scoring batch failed: {str(e)[:150]}")
                limitations.append("Some AI scoring batches failed; those videos use rule-based estimates.")

    for o in screened:
        a = o["ai"]
        if a:
            o["ai_scored"] = True
            if a.get("pillar") in PILLARS:
                o["cluster"] = a["pillar"]
            o["primary_keyword"] = a.get("primary_keyword") or o["primary_keyword"]
            o["secondary_keywords"] = a.get("secondary_keywords") or o["secondary_keywords"]
            o["topic_key"] = (a.get("topic_key") or o["primary_keyword"]).lower().strip()
            o["viewer_problem"], o["transformation"] = a.get("viewer_problem", ""), a.get("transformation", "")
            o["s_align"] = max(0, min(15, int(a.get("alignment", 0))))
            o["shareability"] = a.get("shareability_driver") if a.get("shareability_driver") in SHARE_DRIVERS else "none"
            pk = max(0, min(10, int(a.get("packaging", 0))))
            o["s_pack"] = min(pk, 6) if o["shareability"] == "none" else pk
            o["s_repl"] = max(0, min(10, int(a.get("replicability", 0))))
            o["production_difficulty"] = a.get("production_difficulty", "Medium")
            o["production_cost"] = a.get("production_cost", "Medium")
            o["production_hours"] = a.get("production_hours")
            o["clear_problem"], o["adaptable"] = bool(a.get("clear_problem")), bool(a.get("adaptable"))
            o["original_angle"] = bool(a.get("original_angle_exists"))
            o["evergreen"] = bool(a.get("evergreen"))
            o["ai_exclude"] = bool(a.get("exclude"))
            o["ai_exclude_reason"] = a.get("exclude_reason", "")
            o["ai_pen"] = {k: int((a.get("penalties") or {}).get(k, 0) or 0) for k in
                           ("fame", "saturation", "production", "copying", "loose_alignment", "guest_event")}
        else:
            o["ai_scored"] = False
            o["topic_key"] = o["primary_keyword"]
            o["viewer_problem"], o["transformation"] = "", ""
            hits = classify_topic(o["title"], o["tags"], o["description"], regexes)
            o["s_align"] = 12 if hits and hits[2] >= 2 else 10 if hits and hits[2] == 1 else 7
            o["shareability"] = "none"
            o["s_pack"] = min(6, 3 + bool(re.search(r"\d", o["title"])) + bool(re.search(r"\b(how|why|i tried|stop|mistakes?)\b", o["title"].lower())) + 1)
            o["s_repl"] = 6
            o["production_difficulty"], o["production_cost"], o["production_hours"] = "Medium", "Low", None
            o["clear_problem"], o["adaptable"], o["original_angle"], o["evergreen"] = True, True, True, False
            o["ai_exclude"], o["ai_exclude_reason"] = False, ""
            o["ai_pen"] = {k: 0 for k in ("fame", "saturation", "production", "copying", "loose_alignment", "guest_event")}

    screened = [o for o in screened if not o["ai_exclude"]]

    # ---- 6. trend validation across unrelated channels
    topics = defaultdict(list)
    for o in screened:
        topics[o["topic_key"]].append(o)
    past_weeks = defaultdict(set)
    for r in db.execute("SELECT snapshot_date, data FROM opportunity_snapshots"):
        tk = json.loads(r["data"]).get("topic_key")
        if tk:
            past_weeks[tk].add(r["snapshot_date"])

    def trend_for(tk):
        vids = topics.get(tk, [])
        strong = [v for v in vids if (v["cpm"] or 0) >= 2 or (v["vs_ratio"] or 0) >= 1 or v["vph_pct"] >= 0.75]
        w60 = [v for v in strong if v["age_days"] <= 60]
        ch60 = {v["channel_id"] for v in w60}
        ch100 = {v["channel_id"] for v in strong}
        cpms = [v["cpm"] for v in vids if v["cpm"] is not None]
        if len(vids) >= 8 and cpms and statistics.median(cpms) < 1.5:
            return "Saturated Trend"
        if len(w60) >= 2 and len(ch60) >= 2 and min(v["age_days"] for v in w60) <= 21:
            return "Emerging Trend"
        if len(strong) >= 3 and len(ch100) >= 2:
            return "Validated Trend"
        if len(w60) >= 2 and len(ch60) >= 2:
            return "Emerging Trend"
        if len(past_weeks.get(tk, ())) >= 3 or (any(v["evergreen"] for v in vids) and len(ch100) >= 2):
            return "Evergreen Viral Pattern"
        return "Single-Video Signal"

    trend_cache = {tk: trend_for(tk) for tk in topics}

    # ---- 7. qualification + fixed score + penalties + decision
    qualified = []
    for o in screened:
        o["trend"] = trend_cache.get(o["topic_key"], "Single-Video Signal")
        m = o["momentum"]["label"]
        cond = {
            "Strong VPH for its age": o["vph_pct"] >= 0.75,
            "Views-to-subscriber ratio 0.50+": (o["vs_ratio"] or 0) >= 0.5,
            "Views well above the channel's normal (2x+)": (o["cpm"] or 0) >= 2,
            "Strong result from a Micro/Small/Medium channel": o["size"] in ("Micro", "Small", "Medium") and ((o["cpm"] or 0) >= 2 or (o["vs_ratio"] or 0) >= 1),
            "Continued momentum after day 7": o["age_days"] > 7 and m in ("growing", "sustained"),
            "Strong engagement vs comparable videos": o["eng_rate"] is not None and pool_eng_median and o["eng_rate"] >= pool_eng_median * 1.3,
            "Engagement above the channel's own average": o["eng_rel"] is not None and o["eng_rel"] >= 1.2,
            "High comment volume": o["comment_rate"] is not None and o["comment_rate"] >= max(2 * (o["channel_comment_rate"] or 0), 8),
            "Topic working across unrelated channels": o["trend"] in ("Emerging Trend", "Validated Trend", "Evergreen Viral Pattern"),
            "Fits your channel directly": o["s_align"] >= 10,
            "Clear problem or transformation in the packaging": o["clear_problem"] and o["s_pack"] >= 7,
            "Realistic to produce": o["s_repl"] >= 7 and o["production_difficulty"] != "High",
            "Adaptable without copying": o["adaptable"] and o["original_angle"],
        }
        perf_keys = list(cond)[:5]
        o["conditions_met"] = [k for k, v in cond.items() if v]
        if len(o["conditions_met"]) < 3 or not any(cond[k] for k in perf_keys):
            continue
        # videos over 40 minutes need exceptional evidence and a direct fit
        if o["duration_s"] > 2400 and not ((o["cpm"] or 0) >= 4 and o["s_align"] >= 12 and o["ai_pen"]["guest_event"] == 0 and o["ai_pen"]["fame"] <= 3):
            continue

        s = {"vph": vph_points(o["vph_pct"]), "outlier": outlier_points(o["cpm"], o["eng_rel"]),
             "vs": vs_points(o["vs_ratio"]), "alignment": o["s_align"], "packaging": o["s_pack"],
             "replicability": o["s_repl"], "recency": recency_points(o["stage"], m)}
        data_inc = (4 if o["cpm"] is None else 0) + (3 if o["eng_rate"] is None else 0) + (3 if o["age_days"] < 3 else 0) + (3 if o["subs"] is None else 0)
        decline = 0
        if o["momentum"]["prev_vph"] and o["momentum"]["recent_vph"] is not None:
            ratio = o["momentum"]["recent_vph"] / o["momentum"]["prev_vph"]
            decline = 10 if ratio < 0.4 else 5 if ratio < 0.7 else 0
        pen = dict(o["ai_pen"])
        pen["saturation"] = max(pen.get("saturation", 0), 6 if o["trend"] == "Saturated Trend" else 0)
        pen["data_incomplete"] = data_inc
        pen["momentum_decline"] = decline
        pen = {k: min(PENALTY_CAPS[k], max(0, v)) for k, v in pen.items()}
        o["scores"], o["penalties"] = s, pen
        o["penalty_total"] = sum(pen.values())
        o["score"] = max(0, min(100, sum(s.values()) - o["penalty_total"]))
        conf_pts = (2 if o["cpm"] is not None else 0) + (1 if o["eng_rate"] is not None else 0) + \
                   (1 if o["momentum"]["recent_vph"] is not None else 0) + (1 if o["ai_scored"] else 0)
        o["confidence"] = "Very High" if conf_pts >= 5 else "High" if conf_pts == 4 else "Medium" if conf_pts >= 2 else "Low"
        if not o["ai_scored"] and o["confidence"] in ("High", "Very High"):
            o["confidence"] = "Medium"
        qualified.append(o)

    def finalize_decision(o):
        d = decision_for(o["score"])
        fails = []
        if d == "Produce Now":
            checks = {
                "fits the fixed niche directly": o["s_align"] >= 13,
                "data confidence Medium or better": o["confidence"] in ("Medium", "High", "Very High"),
                "replication difficulty Low or Medium": o["production_difficulty"] in ("Low", "Medium"),
                "audience demand still active": o["momentum"]["label"] != "declining",
                "not dependent on creator fame": o["penalties"]["fame"] <= 3,
                "realistic production": o["penalties"]["production"] <= 3,
                "a meaningful original angle exists": o["original_angle"],
                "no copying risk": o["penalties"]["copying"] <= 3,
                "strong title and thumbnail opportunity": o["s_pack"] >= 7,
                "solves a clear viewer problem": o["clear_problem"],
            }
            fails = [k for k, ok in checks.items() if not ok]
            if fails:
                d = "High-Confidence Opportunity"
        o["decision"], o["produce_now_fails"] = d, fails

    trend_rank = {"Emerging Trend": 4, "Validated Trend": 3, "Evergreen Viral Pattern": 2, "Single-Video Signal": 1, "Saturated Trend": 0}

    def sort_key(o):
        return (o["score"], trend_rank.get(o["trend"], 0), o["shareability"] != "none", (o["vs_ratio"] or 0))

    for o in qualified:
        finalize_decision(o)
    qualified.sort(key=sort_key, reverse=True)

    # ---- 8. deep analysis for Top 10 + rising candidates + exceptional small-channel outliers
    prev = {}
    for r in db.execute("""SELECT s.video_id, s.data FROM opportunity_snapshots s
                           JOIN (SELECT video_id, MAX(snapshot_date) d FROM opportunity_snapshots WHERE snapshot_date<? GROUP BY video_id) m
                           ON m.video_id=s.video_id AND m.d=s.snapshot_date""", (today,)):
        prev[r["video_id"]] = json.loads(r["data"])
    deep_set = [o for o in qualified if o["decision"] != "Skip"][:10]
    for o in qualified[10:20]:
        p = prev.get(o["video_id"])
        if p and o["score"] >= p.get("score", 0) + 5:
            deep_set.append(o)
    deep_set += [o for o in qualified if o["size"] in ("Micro", "Small") and (o["cpm"] or 0) >= 8 and o not in deep_set]
    deep_set = deep_set[:cfg.get("deep_max", 15)]
    transcripts_ok = 0
    for o in qualified:
        o["deep"] = None
    if use_ai:
        for o in deep_set:
            transcript = get_transcript(db, o["video_id"], today)
            transcripts_ok += bool(transcript)
            comments = get_comments(db, yt, o["video_id"], today)
            try:
                o["deep"] = ai_deep(cfg, o, mine, transcript, comments)
                o["hook_basis"] = "transcript" if transcript else "inferred"
                o["comments_seen"] = len(comments)
                pk = o["deep"].get("packaging_score")
                if isinstance(pk, (int, float)):  # re-score packaging now that the full thumbnail was seen
                    pk = max(0, min(10, int(pk)))
                    o["scores"]["packaging"] = min(pk, 6) if o["shareability"] == "none" else pk
                    o["score"] = max(0, min(100, sum(o["scores"].values()) - o["penalty_total"]))
                    o["s_pack"] = o["scores"]["packaging"]
                if transcript and o["confidence"] == "High":
                    o["confidence"] = "Very High"
                finalize_decision(o)
            except Exception as e:
                H.log(f"Deep analysis failed for {o['video_id']}: {str(e)[:150]}")
        db.commit()
        if deep_set and transcripts_ok < len(deep_set):
            limitations.append(f"Transcripts were available for {transcripts_ok} of {len(deep_set)} deeply analysed videos; the other hooks are marked as inferred.")
    qualified.sort(key=sort_key, reverse=True)

    # ---- 9. ranks, weekly status, storage (opportunities + permanent snapshots)
    added = updated = snaps = 0
    seen_ids = set()
    for rank, o in enumerate(qualified, 1):
        o["rank"] = rank
        p = prev.get(o["video_id"])
        o["prev_rank"] = p.get("rank") if p else None
        if not p:
            status = "New"
        else:
            up = (p.get("rank") and rank <= p["rank"] - 3) or o["score"] >= p.get("score", 0) + 5 or (p.get("vph") and o["vph"] >= p["vph"] * 1.2)
            down = (p.get("rank") and rank >= p["rank"] + 3) or o["score"] <= p.get("score", 0) - 5 or (p.get("vph") and o["vph"] <= p["vph"] * 0.8)
            if p.get("status") in ("Falling", "Dropped") and up:
                status = "Recovered"
            elif up and not down:
                status = "Rising"
            elif down and not up:
                status = "Falling"
            else:
                status = "Holding"
        o["status"] = status
        seen_ids.add(o["video_id"])

    top_ids = [o["video_id"] for o in qualified if o["decision"] != "Skip"][:10]
    for o in qualified:
        rec = record_from(o, today, top_ids)
        old = db.execute("SELECT first_discovered FROM opportunities WHERE video_id=?", (o["video_id"],)).fetchone()
        rec["first_discovered"] = old["first_discovered"] if old else today
        if old:
            updated += 1
            if not rec.get("deep"):  # keep the last deep analysis if this week's was not run
                prev_rec = json.loads(db.execute("SELECT data FROM opportunities WHERE video_id=?", (o["video_id"],)).fetchone()["data"])
                if prev_rec.get("deep"):
                    rec["deep"], rec["deep_date"] = prev_rec["deep"], prev_rec.get("deep_date")
        else:
            added += 1
        db.execute("INSERT OR REPLACE INTO opportunities VALUES(?,?,?,?,?,?,?,?)",
                   (o["video_id"], json.dumps(rec, ensure_ascii=False), o["status"], o["rank"], o["score"],
                    o["decision"], rec["first_discovered"], today))
        db.execute("INSERT OR REPLACE INTO opportunity_snapshots VALUES(?,?,?)",
                   (o["video_id"], today, json.dumps(snapshot_from(rec))))
        snaps += 1

    # previously tracked videos that no longer qualify -> Dropped (snapshot kept, never overwritten)
    for r in db.execute("SELECT video_id, data FROM opportunities").fetchall():
        if r["video_id"] in seen_ids:
            continue
        rec = json.loads(r["data"])
        pub = H.parse_date(rec.get("published"))
        if pub and (now - pub).days > 100:
            continue
        rec.update(status="Dropped", prev_rank=rec.get("rank"), rank=None, last_checked=today, in_top10=False)
        db.execute("UPDATE opportunities SET data=?, status='Dropped', rank=NULL, last_checked=? WHERE video_id=?",
                   (json.dumps(rec, ensure_ascii=False), today, r["video_id"]))
        db.execute("INSERT OR REPLACE INTO opportunity_snapshots VALUES(?,?,?)",
                   (r["video_id"], today, json.dumps(snapshot_from(rec))))
        snaps += 1
    db.commit()

    # ---- 10. execution summary, report, email
    week_ago = (dt.date.fromisoformat(today) - dt.timedelta(days=7)).isoformat()
    logs = db.execute("SELECT unit, ids FROM search_log WHERE day>?", (week_ago,)).fetchall()
    all_ids = [i for r in logs for i in (r["ids"] or "").split(",") if i]
    clusters_searched = {u.split("|")[3] for u in (r["unit"] for r in logs) if u.count("|") >= 3}
    summary.update(
        clusters_searched=len(clusters_searched), keyword_searches=len(logs), result_batches=len(logs),
        duplicates_removed=len(all_ids) - len(set(all_ids)), eligible_pool=len(pool),
        passing_qualification=len(qualified), added=added, updated=updated, snapshots=snaps,
        produce_now=sum(1 for o in qualified if o["decision"] == "Produce Now"),
        high_confidence=sum(1 for o in qualified if o["decision"] == "High-Confidence Opportunity"),
        deep_analysed=sum(1 for o in qualified if o.get("deep")), ai_used=use_ai)

    recs = [json.loads(r["data"]) for r in db.execute("SELECT data FROM opportunities WHERE last_checked=? AND status<>'Dropped' ORDER BY rank", (today,))]
    dropped = [json.loads(r["data"]) for r in db.execute("SELECT data FROM opportunities WHERE last_checked=? AND status='Dropped'", (today,))]
    report_html = build_report(cfg, today, recs, dropped, summary, trend_cache)
    rep_dir = ROOT / "data" / "reports"
    rep_dir.mkdir(parents=True, exist_ok=True)
    (rep_dir / f"{today}.html").write_text(report_html, encoding="utf-8")
    summary["report"] = f"reports/{today}.html"
    summary["email_sent"] = send_email(cfg, today, report_html, recs)
    if not summary["email_sent"]:
        limitations.append("Email was not sent (see run log). The report is saved in the dashboard under Opportunities and in data/reports/.")
    db.execute("INSERT OR REPLACE INTO weekly_runs VALUES(?,?)", (today, json.dumps(summary)))
    db.commit()
    H.log(f"Weekly opportunities: {len(qualified)} qualified, {summary['produce_now']} Produce Now, report saved")


# --------------------------------------------------------------------------- records
def production_time_cat(hours):
    if hours is None:
        return "Unavailable"
    return "Low" if hours < 8 else "Medium" if hours <= 24 else "High"


def record_from(o, today, top_ids):
    deep = o.get("deep") or {}
    recm = deep.get("recommendation") or {}
    ev = {"views": "Verified", "subs": "Verified" if o["subs"] is not None else "Unavailable", "likes": "Verified",
          "comments": "Verified", "duration": "Verified", "published": "Verified", "vph": "Calculated",
          "vs_ratio": "Calculated" if o["vs_ratio"] is not None else "Unavailable",
          "cpm": "Calculated" if o["cpm"] is not None else "Unavailable",
          "eng_rate": "Calculated" if o["eng_rate"] is not None else "Unavailable",
          "vidiq": "Unavailable", "alignment": "Inferred" if o["ai_scored"] else "Inferred (rule-based)",
          "packaging": "Inferred" if o["ai_scored"] else "Inferred (rule-based)",
          "replicability": "Inferred" if o["ai_scored"] else "Inferred (rule-based)",
          "hook": ("Verified transcript" if o.get("hook_basis") == "transcript" else "Inferred") if deep else "Unavailable"}
    return {
        "video_id": o["video_id"], "title": o["title"], "url": o["url"], "thumb": o["thumb"],
        "channel": o["channel"], "channel_url": o["channel_url"], "size": o["size"], "subs": o["subs"],
        "published": o["published"].isoformat(), "age_days": o["age_days"], "duration_s": o["duration_s"],
        "views": o["views"], "likes": o["likes"], "comments": o["comments"], "vph": o["vph"],
        "eng_rate_pct": o["eng_rate_pct"], "channel_eng_pct": o["channel_eng_pct"], "vs_ratio": o["vs_ratio"],
        "vidiq": None, "cpm": o["cpm"], "cpm_class": o["cpm_class"], "baseline_n": o["baseline_n"],
        "cluster": o["cluster"], "primary_keyword": o["primary_keyword"], "secondary_keywords": o["secondary_keywords"],
        "topic_key": o["topic_key"], "viewer_problem": o["viewer_problem"] or (deep.get("why_working") or {}).get("audience_problem", ""),
        "transformation": o["transformation"], "stage": o["stage"], "trend": o["trend"], "status": o["status"],
        "rank": o["rank"], "prev_rank": o["prev_rank"], "in_top10": o["video_id"] in top_ids,
        "score": o["score"], "scores": o["scores"], "penalties": o["penalties"], "penalty_total": o["penalty_total"],
        "decision": o["decision"], "produce_now_fails": o["produce_now_fails"], "conditions_met": o["conditions_met"],
        "shareability": o["shareability"], "production_difficulty": recm.get("difficulty") or o["production_difficulty"],
        "production_cost": recm.get("cost") or o["production_cost"],
        "production_hours": recm.get("hours") or o["production_hours"],
        "production_time": production_time_cat(recm.get("hours") or o["production_hours"]),
        "confidence": o["confidence"], "momentum": o["momentum"], "evidence": ev,
        "recommended_title": recm.get("title", ""), "thumbnail_concept": recm.get("thumbnail_concept", ""),
        "deep": deep or None, "deep_date": today if deep else None, "last_checked": today,
        "ai_scored": o["ai_scored"],
    }


def snapshot_from(rec):
    keys = ("video_id", "title", "rank", "prev_rank", "status", "views", "vph", "subs", "vs_ratio", "eng_rate_pct",
            "vidiq", "cpm", "score", "stage", "trend", "decision", "topic_key")
    return {k: rec.get(k) for k in keys}


# --------------------------------------------------------------------------- dashboard data
def dashboard_data(db):
    try:
        run = db.execute("SELECT day, summary FROM weekly_runs ORDER BY day DESC LIMIT 1").fetchone()
    except Exception:
        return None
    if not run:
        return None
    day = run["day"]
    recs = [json.loads(r["data"]) for r in db.execute(
        "SELECT data FROM opportunities WHERE last_checked=? ORDER BY rank IS NULL, rank", (day,))]
    hist = defaultdict(list)
    for r in db.execute("SELECT video_id, snapshot_date, data FROM opportunity_snapshots ORDER BY snapshot_date"):
        d = json.loads(r["data"])
        hist[r["video_id"]].append({"date": r["snapshot_date"], "rank": d.get("rank"), "score": d.get("score"),
                                    "vph": d.get("vph"), "views": d.get("views"), "status": d.get("status")})
    for rec in recs:
        rec["history"] = hist.get(rec["video_id"], [])[-12:]
    reports = [r["day"] for r in db.execute("SELECT day FROM weekly_runs ORDER BY day DESC LIMIT 26")]
    mine = db.execute("SELECT data FROM my_channel ORDER BY day DESC LIMIT 1").fetchone()
    return {"day": day, "summary": json.loads(run["summary"]), "items": recs, "reports": reports,
            "my_channel": json.loads(mine["data"]) if mine else None}


# --------------------------------------------------------------------------- report + email
def e(x):
    return html.escape("" if x is None else str(x))


def fmt_num(n):
    if n is None:
        return "Unavailable"
    n = float(n)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= div:
            return f"{n / div:.1f}{suf}"
    return f"{n:.0f}" if n == int(n) else f"{n:.2f}"


def opp_block(o, idx, full=False):
    d = o.get("deep") or {}
    r = d.get("recommendation") or {}
    w = d.get("why_working") or {}
    en = d.get("engagement") or {}
    ev = o.get("evidence", {})
    rows = [
        ("Weekly status", o.get("status")), ("Source video", f'<a href="{e(o["url"])}">{e(o["title"])}</a>'),
        ("Channel", f'{e(o["channel"])}, {e(o["size"])} ({fmt_num(o["subs"])} subscribers)'),
        ("Published", f'{e(o["published"][:10])} ({e(o["age_days"])} days ago), {round(o["duration_s"] / 60, 1)} min'),
        ("Total views", f'{fmt_num(o["views"])} (Verified)'), ("Current VPH", f'{fmt_num(o["vph"])} (Calculated)'),
        ("Views ÷ subscribers", f'{e(o["vs_ratio"]) if o["vs_ratio"] is not None else "Unavailable"} ({ev.get("vs_ratio")})'),
        ("vidIQ outlier score", "Unavailable"),
        ("Channel Performance Multiple", f'{e(o["cpm"])}× ({e(o["cpm_class"])}, {ev.get("cpm")})' if o["cpm"] is not None else "Unavailable"),
        ("Opportunity Score", f'<b>{e(o["score"])}</b> / 100 ({e(o["decision"])})'), ("Data confidence", o["confidence"]),
        ("Core topic", f'{e(o["cluster"])}: {e(o["primary_keyword"])}'),
        ("Viewer problem", o.get("viewer_problem") or w.get("audience_problem") or "Unavailable"),
        ("Why it's outperforming", w.get("main_driver") or w.get("why_unusual") or "; ".join(o.get("conditions_met", [])[:4])),
        ("Why it fits your channel", f'Alignment {o["scores"]["alignment"]}/15 ({ev.get("alignment")})'),
        ("Why it's still active", w.get("still_active") or f'Momentum: {o["momentum"]["label"]}'),
        ("Engagement and shareability", f'{e(en.get("read", "Unavailable"))}; driver: {e(en.get("shareability_driver") or o.get("shareability"))}'),
        ("Your original concept", r.get("concept", "Needs deep analysis (AI key required)")),
        ("Title", r.get("title", "")), ("Backup title", r.get("backup_title", "")),
        ("Thumbnail", f'{e(r.get("thumbnail_concept", ""))} — text: “{e(r.get("thumbnail_text", ""))}”' if r else ""),
        ("Opening hook", r.get("hook", "")), ("Length", r.get("length", "")),
        ("Production", f'{e(o["production_difficulty"])} difficulty, {e(o["production_cost"])} cost, {e(o.get("production_hours") or "?")} hours'),
        ("Main risk", r.get("risk", "")), ("Decision", o["decision"]),
    ]
    if full:
        rows += [("Why it ranks first", r.get("why_worth_it", "")), ("Evidence", r.get("evidence", "")),
                 ("Structure", "<br>".join(e(s) for s in r.get("sections", []))), ("How to differentiate", r.get("differentiation", "")),
                 ("Confidence", r.get("confidence", o["confidence"]))]
    # values for these keys were assembled above with e() already; everything else is escaped here
    html_keys = {"Source video", "Channel", "Published", "Opportunity Score", "Thumbnail", "Production", "Structure",
                 "Engagement and shareability", "Channel Performance Multiple", "Views ÷ subscribers",
                 "Total views", "Current VPH"}
    trs = "".join(
        f'<tr><td style="padding:4px 10px 4px 0;color:#5f6b63;vertical-align:top;white-space:nowrap">{e(k)}</td>'
        f'<td style="padding:4px 0">{v if k in html_keys else e(v)}</td></tr>'
        for k, v in rows if v not in (None, ""))
    return f'<h3 style="margin:22px 0 6px;font-size:17px">{idx}</h3><table style="border-collapse:collapse;font-size:14px">{trs}</table>'


def build_report(cfg, today, recs, dropped, summary, trends):
    top = [o for o in recs if o["decision"] != "Skip"]
    best5 = top[:5]
    url = cfg.get("dashboard_url", "")
    parts = [f'<div style="font-family:Arial,sans-serif;max-width:760px;color:#1e2b24;line-height:1.45">',
             f'<h1 style="font-size:22px">Weekly YouTube Opportunities — {e(today)}</h1>']
    if not best5:
        parts.append("<p>No video met the qualification rules this week. This usually means there is not enough data yet "
                     "(the first full week of daily collection) or the YouTube quota ran out. Nothing was forced into the list.</p>")
    else:
        parts.append('<h2 style="font-size:19px;background:#f2b634;padding:6px 10px;border-radius:6px">The video to make this week</h2>')
        parts.append(opp_block(best5[0], "1. " + e(best5[0].get("recommended_title") or best5[0]["title"]), full=True))
        parts.append('<h2 style="font-size:19px;margin-top:30px">Best 5 opportunities</h2>')
        for i, o in enumerate(best5, 1):
            parts.append(opp_block(o, f'{i}. {e(o.get("recommended_title") or o["title"])}'))
    backups = top[5:7]
    avoid = next((tk for tk, t in trends.items() if t == "Saturated Trend"), None)
    falling = next((o for o in recs if o["status"] == "Falling"), None) or (dropped[0] if dropped else None)
    emerging = next((o for o in top[5:] if o["trend"] == "Emerging Trend"), None)
    parts.append('<h2 style="font-size:19px;margin-top:30px">Also this week</h2><ul>')
    for i, b in enumerate(backups, 1):
        parts.append(f'<li>Backup choice {i}: <a href="{e(b["url"])}">{e(b["title"])}</a> — score {e(b["score"])}</li>')
    parts.append(f'<li>Topic to avoid: {e(avoid) if avoid else "none flagged as saturated this week"}</li>')
    parts.append(f'<li>Losing momentum: {e(falling["title"]) + " (" + e(falling["status"]) + ")" if falling else "needs at least two weekly runs to detect"}</li>')
    parts.append(f'<li>Emerging pattern to monitor: {e(emerging["topic_key"]) + " — " + e(emerging["title"]) if emerging else "none outside the Best 5"}</li>')
    if url:
        parts.append(f'<li>Full research database: <a href="{e(url)}#opportunities">{e(url)}#opportunities</a></li>')
    parts.append("</ul>")
    s = summary
    parts.append('<h2 style="font-size:17px;margin-top:30px">Execution summary</h2><p style="font-size:13px;color:#5f6b63">'
                 f'{s.get("clusters_searched", 0)} topic clusters searched; {s.get("keyword_searches", 0)} keyword searches '
                 f'({s.get("result_batches", 0)} result batches) in the last 7 days; {s.get("videos_reviewed", 0)} videos reviewed; '
                 f'{s.get("duplicates_removed", 0)} duplicate search results removed; {s.get("irrelevant_removed", 0)} ineligible or off-niche videos removed; '
                 f'{s.get("passing_qualification", 0)} passed qualification; {s.get("added", 0)} added and {s.get("updated", 0)} updated in the database; '
                 f'{s.get("snapshots", 0)} history snapshots; {s.get("produce_now", 0)} Produce Now; {s.get("high_confidence", 0)} High-Confidence.</p>'
                 '<p style="font-size:13px;color:#5f6b63">Limitations: ' + " ".join(e(x) for x in s.get("limitations", [])) + '</p></div>')
    return "".join(parts)


def send_email(cfg, today, report_html, recs):
    user = os.environ.get("SMTP_USER", "").strip()
    pw = os.environ.get("SMTP_APP_PASSWORD", "").strip()
    to = os.environ.get("REPORT_EMAIL_TO", "").strip()
    if not (user and pw and to):
        H.log("Weekly email skipped: SMTP_USER, SMTP_APP_PASSWORD and REPORT_EMAIL_TO secrets are not all set")
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Weekly YouTube Opportunities — Best 5 Videos to Make — {today}"
    msg["From"], msg["To"] = user, to
    text = "\n".join(f"{o['rank']}. {o['title']} — score {o['score']} — {o['url']}" for o in recs if o["decision"] != "Skip")[:5000]
    msg.attach(MIMEText(text or "No qualifying opportunities this week.", "plain", "utf-8"))
    msg.attach(MIMEText(report_html, "html", "utf-8"))
    try:
        with smtplib.SMTP_SSL(cfg.get("smtp_host", "smtp.gmail.com"), cfg.get("smtp_port", 465),
                              context=ssl.create_default_context(), timeout=60) as s:
            s.login(user, pw)
            s.sendmail(user, [to], msg.as_string())
        H.log("Weekly email sent")
        return True
    except Exception as ex:
        H.log(f"Weekly email failed: {type(ex).__name__}: {str(ex)[:120]}")
        return False
