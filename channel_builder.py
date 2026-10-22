"""
Channel Builder
---------------
Everything a top creator uses to choose topics, in one place:

  Daily (cheap):
    * My channel: your uploads, views growth, your own outliers (views vs your median)
    * Studio data (optional, needs a one-time Google sign-in): retention, search terms that
      bring people to you, traffic sources, impressions/CTR when the API provides them

  Weekly on "builder day" (Wednesday by default):
    * Remake finder: evergreen outliers 6 months to 5 years old in your pillars
    * Content gaps: real searches (YouTube autocomplete) whose top results are old, weak or Shorts
    * Format scout: title formats breaking out in neighbouring niches
    * Title-format lift: which title structures over-perform inside your niche right now
    * AI: sequels to your outliers, remake angles, format adaptations, and 10 titles +
      3 thumbnail concepts for each top idea in the Idea bank
    * Season calendar: moments coming up, with "start by" and "publish by" dates

Numbers come straight from YouTube (Verified) or are calculated from them; anything an AI wrote is
marked Inferred. Nothing is invented when data is missing.
"""
import datetime as dt
import json
import math
import os
import re
import statistics
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

import opportunity_engine as oe

H = None  # helpers from tracker.py

SCHEMA = """
CREATE TABLE IF NOT EXISTS my_video_stats(video_id TEXT, day TEXT, title TEXT, published TEXT, duration_s INTEGER,
  views INTEGER, likes INTEGER, comments INTEGER, PRIMARY KEY(video_id, day));
CREATE TABLE IF NOT EXISTS studio(day TEXT, kind TEXT, data TEXT, PRIMARY KEY(day, kind));
CREATE TABLE IF NOT EXISTS remakes(video_id TEXT PRIMARY KEY, query TEXT, first_seen TEXT, last_seen TEXT);
CREATE TABLE IF NOT EXISTS gaps(phrase TEXT PRIMARY KEY, data TEXT, day TEXT);
CREATE TABLE IF NOT EXISTS scout(video_id TEXT PRIMARY KEY, query TEXT, day TEXT);
CREATE TABLE IF NOT EXISTS ideas(id TEXT PRIMARY KEY, data TEXT, day TEXT, first_day TEXT);
CREATE TABLE IF NOT EXISTS ai_notes(kind TEXT, key TEXT, data TEXT, day TEXT, PRIMARY KEY(kind, key));
CREATE TABLE IF NOT EXISTS builder_runs(day TEXT PRIMARY KEY, summary TEXT);
"""

TITLE_FORMATS = [
    ("I tried X for N days", r"\bi tried\b|\b\d+\s*(day|week|month)s?\s+(of|challenge)\b|\bfor \d+\s*(days?|weeks?|months?)\b"),
    ("Numbered list", r"^\s*\d+\s|\b\d+\s+(things|habits|ways|rules|lessons|mistakes|signs|tips|steps|books|apps|tools)\b"),
    ("Why you… / why your…", r"^why (you|your|most|people)\b"),
    ("How I…", r"^how i\b"),
    ("How to… (with a condition)", r"^how to\b.*\b(even if|without|when you|in \d+|if you)\b"),
    ("How to… (plain)", r"^how to\b"),
    ("Stop / don't…", r"^(stop|don'?t|quit|never)\b"),
    ("Hidden truth", r"\b(nobody|no one) (tells|talks|told)\b|\bthe truth about\b|\bhonest\b|\bsecret\b"),
    ("I was wrong / regret", r"\bi was wrong\b|\bchanged my mind\b|\bi regret\b|\bi wish i\b"),
    ("Versus / comparison", r"\bvs\.?\b|\bversus\b"),
    ("Lazy / easy / simple way", r"\b(lazy|easiest|easy|simple|realistic)\b"),
    ("Identity call-out", r"\bif you'?re\b|\bpeople who\b|\bfor (people|students|introverts|beginners)\b"),
    ("Transformation", r"\bchanged my life\b|\btransform|\bbefore and after\b|\bbecame\b|\bnew (me|person)\b"),
    ("Time-bound promise", r"\bin (\d+|one|a|an) (minutes?|days?|weeks?|hours?|month)\b"),
    ("Life stage / age", r"\b(in your|at) \d{2}s?\b|\b(20s|30s|teens?|college|uni)\b"),
    ("Question", r"\?\s*$"),
    ("System / framework", r"\b(system|framework|method|blueprint|routine)\b"),
]
TITLE_FORMATS_RX = [(n, re.compile(p)) for n, p in TITLE_FORMATS]


def title_formats(title):
    t = (title or "").lower().strip()
    found = [n for n, rx in TITLE_FORMATS_RX if rx.search(t)]
    if "How to… (with a condition)" in found and "How to… (plain)" in found:
        found.remove("How to… (plain)")
    return found


def ensure_schema(db):
    db.executescript(SCHEMA)
    db.commit()


def due(cfg, db, today, force):
    if db.execute("SELECT 1 FROM builder_runs WHERE day=?", (today,)).fetchone():
        return False
    return force or dt.date.fromisoformat(today).weekday() == cfg.get("builder", {}).get("weekday", 2)


# --------------------------------------------------------------------------- small HTTP helpers (OAuth)
def _form_post(url, form):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(), method="POST",
                                 headers={"content-type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:200]}") from None


def _get_json(url, params, token):
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params), headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:200]}") from None


def studio_connected():
    return all(os.environ.get(k, "").strip() for k in ("YT_OAUTH_CLIENT_ID", "YT_OAUTH_CLIENT_SECRET", "YT_OAUTH_REFRESH_TOKEN"))


# --------------------------------------------------------------------------- daily: my channel
def run_daily(cfg, db, yt, today, helpers):
    global H
    H = oe.H = helpers
    ensure_schema(db)
    cid = cfg.get("my_channel", {}).get("channel_id")
    if not cid:
        return
    try:
        d = yt.call("channels", part="snippet,statistics", id=cid)
        it = (d.get("items") or [{}])[0]
        st = it.get("statistics", {})
        db.execute("INSERT OR REPLACE INTO studio VALUES(?,?,?)", (today, "channel_public", json.dumps({
            "title": it.get("snippet", {}).get("title"), "subs": None if st.get("hiddenSubscriberCount") else int(st.get("subscriberCount", 0)),
            "views": int(st.get("viewCount", 0)), "videos": int(st.get("videoCount", 0))})))
        pl = yt.call("playlistItems", part="contentDetails", playlistId="UU" + cid[2:], maxResults=50)
        ids = [x["contentDetails"]["videoId"] for x in pl.get("items", [])]
        if ids:
            vd = yt.call("videos", part="snippet,statistics,contentDetails", id=",".join(ids), maxResults=50)
            for v in vd.get("items", []):
                s = v.get("statistics", {})
                db.execute("INSERT OR REPLACE INTO my_video_stats VALUES(?,?,?,?,?,?,?,?)",
                           (v["id"], today, v["snippet"].get("title"), v["snippet"].get("publishedAt"),
                            H.iso_duration(v.get("contentDetails", {}).get("duration")), int(s.get("viewCount", 0)),
                            int(s.get("likeCount", 0)), int(s.get("commentCount", 0))))
        db.commit()
    except H.QuotaBudgetExceeded:
        H.log("Quota budget reached before your channel's videos were loaded")
    except Exception as e:
        H.log(f"My channel update failed: {str(e)[:120]}")
    if studio_connected():
        try:
            fetch_studio(cfg, db, today)
        except Exception as e:
            H.log(f"Studio data failed: {str(e)[:200]}")


def fetch_studio(cfg, db, today):
    tok = _form_post("https://oauth2.googleapis.com/token", {
        "client_id": os.environ["YT_OAUTH_CLIENT_ID"].strip(), "client_secret": os.environ["YT_OAUTH_CLIENT_SECRET"].strip(),
        "refresh_token": os.environ["YT_OAUTH_REFRESH_TOKEN"].strip(), "grant_type": "refresh_token"})["access_token"]
    end = (dt.date.fromisoformat(today) - dt.timedelta(days=2)).isoformat()  # Studio data lags 1-2 days
    start28 = (dt.date.fromisoformat(today) - dt.timedelta(days=30)).isoformat()
    start90 = (dt.date.fromisoformat(today) - dt.timedelta(days=92)).isoformat()
    url = "https://youtubeanalytics.googleapis.com/v2/reports"
    base = {"ids": "channel==MINE", "endDate": end}
    queries = {
        "videos90": dict(base, startDate=start90, dimensions="video", sort="-views", maxResults=50,
                         metrics="views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,subscribersGained,likes,comments,shares"),
        "search_terms28": dict(base, startDate=start28, dimensions="insightTrafficSourceDetail",
                               filters="insightTrafficSourceType==YT_SEARCH", metrics="views", sort="-views", maxResults=25),
        "traffic28": dict(base, startDate=start28, dimensions="insightTrafficSourceType", metrics="views,estimatedMinutesWatched", sort="-views"),
        "daily28": dict(base, startDate=start28, dimensions="day", metrics="views,subscribersGained,subscribersLost,estimatedMinutesWatched", sort="day"),
        "impressions90": dict(base, startDate=start90, dimensions="video", sort="-videoThumbnailImpressions", maxResults=50,
                              metrics="videoThumbnailImpressions,videoThumbnailImpressionsClickRate"),
    }
    ok = 0
    for kind, params in queries.items():
        try:
            d = _get_json(url, params, tok)
            cols = [c["name"] for c in d.get("columnHeaders", [])]
            rows = [dict(zip(cols, r)) for r in d.get("rows", [])]
            db.execute("INSERT OR REPLACE INTO studio VALUES(?,?,?)", (today, kind, json.dumps(rows)))
            ok += 1
        except Exception as e:
            db.execute("INSERT OR REPLACE INTO studio VALUES(?,?,?)", (today, kind, json.dumps({"error": str(e)[:200]})))
            if kind != "impressions90":
                H.log(f"Studio report {kind} failed: {str(e)[:150]}")
    db.commit()
    H.log(f"Studio data: {ok} of {len(queries)} reports loaded")


# --------------------------------------------------------------------------- weekly builder day
def rotate(db, keys, n, prefix):
    last = {r["keyword"]: r["last_day"] for r in db.execute("SELECT keyword, last_day FROM keyword_runs WHERE keyword LIKE ?", (prefix + "%",))}
    return sorted(keys, key=lambda k: last.get(prefix + k, ""))[:n]


def mark(db, key, today):
    db.execute("INSERT OR REPLACE INTO keyword_runs VALUES(?,?)", (key, today))


def run_builder(cfg, db, yt, today, helpers):
    global H
    H = oe.H = helpers
    ensure_schema(db)
    b = cfg.get("builder", {})
    now = H.now_utc()
    summary = {"day": today, "remake_searches": 0, "gap_checks": 0, "scout_searches": 0, "limitations": []}

    def search(**kw):
        d = yt.call("search", part="snippet", type="video", regionCode=cfg["region"], relevanceLanguage=cfg["language"], **kw)
        return [it["id"]["videoId"] for it in d.get("items", []) if it.get("id", {}).get("videoId")]

    try:
        # 1) Remake finder: 6 months - 5 years old, biggest first, rotating through your pillar queries
        rq = b.get("remake_queries") or [q for cl in cfg["clusters"] for q in cl["queries"][:2]]
        cand = defaultdict(set)
        for q in rotate(db, rq, b.get("remake_searches_per_run", 16), "remake|"):
            ids = search(q=q, order="viewCount", videoDuration="medium", maxResults=50,
                         publishedAfter=(now - dt.timedelta(days=365 * 5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         publishedBefore=(now - dt.timedelta(days=180)).strftime("%Y-%m-%dT%H:%M:%SZ"))
            for i in ids:
                cand[i].add(f"remake:{q}")
                db.execute("INSERT INTO remakes VALUES(?,?,?,?) ON CONFLICT(video_id) DO UPDATE SET last_seen=excluded.last_seen",
                           (i, q, today, today))
            mark(db, "remake|" + q, today)
            summary["remake_searches"] += 1
        # 2) Format scout: neighbouring niches, last 30 days
        for q in rotate(db, b.get("scout_queries", []), b.get("scout_searches_per_run", 8), "scout|"):
            ids = search(q=q, order="viewCount", videoDuration="medium", maxResults=50,
                         publishedAfter=(now - dt.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"))
            for i in ids:
                cand[i].add(f"scout:{q}")
                db.execute("INSERT OR REPLACE INTO scout VALUES(?,?,?)", (i, q, today))
            mark(db, "scout|" + q, today)
            summary["scout_searches"] += 1
        H.store_videos(db, yt, cand, today)
        # 3) Content gaps: real autocomplete searches, checked against what currently ranks
        latest = db.execute("SELECT MAX(day) FROM suggestions").fetchone()[0]
        phrases = [r["suggestion"] for r in db.execute("SELECT DISTINCT suggestion FROM suggestions WHERE day=?", (latest,))] if latest else []
        recent_cut = (dt.date.fromisoformat(today) - dt.timedelta(days=28)).isoformat()
        checked = {r["phrase"] for r in db.execute("SELECT phrase FROM gaps WHERE day>=?", (recent_cut,))}
        phrases = [p for p in phrases if p not in checked]
        for ph in rotate(db, phrases, b.get("gap_checks_per_run", 12), "gap|"):
            ids = search(q=ph, order="relevance", maxResults=10)
            got = defaultdict(set)
            for i in ids:
                got[i].add(f"gap:{ph}")
            H.store_videos(db, yt, got, today)
            db.execute("INSERT OR REPLACE INTO gaps VALUES(?,?,?)", (ph, json.dumps(gap_metrics(db, ph, ids, now)), today))
            mark(db, "gap|" + ph, today)
            summary["gap_checks"] += 1
        db.commit()
    except H.QuotaBudgetExceeded:
        summary["limitations"].append("YouTube quota ran out during builder searches; the rest continue next week.")
        db.commit()

    # 4) AI: sequels, remake angles, format adaptations, idea packaging
    data = dashboard_data(cfg, db, today, with_ideas=False)
    if oe.ai_available():
        ai_notes(cfg, db, today, data, summary)
    else:
        summary["limitations"].append("No AI key: sequels, remake angles, format adaptations and title/thumbnail options are skipped.")
    build_ideas(cfg, db, today)
    db.execute("INSERT OR REPLACE INTO builder_runs VALUES(?,?)", (today, json.dumps(summary)))
    db.commit()
    H.log(f"Channel builder: {summary['remake_searches']} remake searches, {summary['gap_checks']} gap checks, {summary['scout_searches']} scout searches")


def gap_metrics(db, phrase, ids, now):
    rows = []
    for i in ids:
        r = db.execute("""SELECT v.id, v.title, v.published_at, v.duration_s, s.views, c.subs, c.title ctitle
                          FROM videos v JOIN video_stats s ON s.video_id=v.id AND s.day=(SELECT MAX(day) FROM video_stats WHERE video_id=v.id)
                          LEFT JOIN channels c ON c.id=v.channel_id WHERE v.id=?""", (i,)).fetchone()
        if r:
            pub = H.parse_date(r["published_at"])
            rows.append({"id": r["id"], "title": r["title"], "views": r["views"] or 0, "subs": r["subs"], "channel": r["ctitle"],
                         "age_days": (now - pub).days if pub else None, "short": (r["duration_s"] or 0) < 180})
    if not rows:
        return {"phrase": phrase, "results": 0}
    views = [x["views"] for x in rows]
    ages = [x["age_days"] for x in rows if x["age_days"] is not None]
    med_views, med_age = statistics.median(views), (statistics.median(ages) if ages else None)
    recent = sum(1 for a in ages if a <= 365)
    shorts = sum(1 for x in rows if x["short"])
    beatable = sum(1 for x in rows if x["subs"] is not None and x["subs"] < 100_000 and x["views"] > 2 * max(x["subs"], 1))
    big = sum(1 for x in rows if (x["subs"] or 0) >= 1_000_000)
    demand = max(0, min(40, (math.log10(max(med_views, 1)) - 3) / 3 * 40))  # 1K views -> 0, 1M -> 40
    stale = 0 if med_age is None else 30 if med_age >= 1095 else 22 if med_age >= 730 else 12 if med_age >= 365 else 4
    stale = max(0, stale - recent * 2)
    beat = max(0, min(30, beatable * 6 + shorts * 3 - big * 4 + 10))
    top = max(rows, key=lambda x: x["views"])
    return {"phrase": phrase, "results": len(rows), "gap_score": round(demand + stale + beat), "median_views": med_views,
            "median_age_days": med_age, "recent_results": recent, "shorts_in_results": shorts, "small_channel_winners": beatable,
            "big_channels": big, "top_result": {"id": top["id"], "title": top["title"], "views": top["views"], "channel": top["channel"]}}


# --------------------------------------------------------------------------- AI notes
def ai_notes(cfg, db, today, data, summary):
    mine_ctx = oe.channel_context(cfg, None)
    jobs = []
    if data["mine"] and data["mine"]["outliers"]:
        jobs.append(("sequel", [{"key": v["id"], "title": v["title"], "views": v["views"], "multiple": v["multiple"]} for v in data["mine"]["outliers"][:4]],
                     "These are the creator's OWN outlier videos (views far above their median). For each, propose 3 follow-up videos: "
                     "a sequel, a deeper next step, and an opposite or contrarian angle. Fields: key, ideas: [{type, title, why}]"))
    if data["remakes"]:
        jobs.append(("remake", [{"key": v["id"], "title": v["title"], "views": v["views"], "age_years": v["age_years"], "channel": v["channel"]} for v in data["remakes"][:8]],
                     "These are evergreen videos from past years that still prove demand. For each, propose a fresh 2026 remake for this "
                     "creator that is clearly better and original (not a copy). Fields: key, angle, title, what_to_improve"))
    if data["scout"]:
        jobs.append(("format", [{"key": v["id"], "title": v["title"], "niche_query": v["query"], "views_to_subs": v["vs_ratio"]} for v in data["scout"][:10]],
                     "These titles broke out in NEIGHBOURING niches. For each, extract the reusable title formula (with placeholders) and "
                     "write 2 adaptations for the creator's pillars. Fields: key, formula, adaptations: [title]"))
    for kind, items, task in jobs:
        prompt = f"""You help a YouTube creator choose topics.
CREATOR CONTEXT
{mine_ctx}

TASK: {task}
Never invent numbers. Titles must be original, not copies.
ITEMS: {json.dumps(items, ensure_ascii=False)}
Return JSON: {{"items": [...]}}"""
        try:
            res = oe.ai_json(cfg, prompt)
            for it in res.get("items", []):
                db.execute("INSERT OR REPLACE INTO ai_notes VALUES(?,?,?,?)", (kind, str(it.get("key")), json.dumps(it, ensure_ascii=False), today))
        except Exception as e:
            summary["limitations"].append(f"AI {kind} notes failed: {str(e)[:80]}")
    db.commit()


def package_ideas(cfg, db, today, ideas):
    """10 titles + 3 thumbnail concepts + one-line promise for the top ideas that don't have them yet."""
    todo = [i for i in ideas if not i.get("titles")][:cfg.get("builder", {}).get("package_ideas", 12)]
    for k in range(0, len(todo), 6):
        batch = todo[k:k + 6]
        prompt = f"""You are a top-1% YouTube packaging strategist.
CREATOR CONTEXT
{oe.channel_context(cfg, None)}

For each video idea, write: promise (one sentence: the concrete outcome the viewer gets), 10 title options
(varied formulas, under 60 characters, curiosity without clickbait, no copying of source titles), and 3 thumbnail
concepts (each: visual, text of 4 words or fewer, emotion). Also say why_click in one line.
IDEAS: {json.dumps([{"key": i["id"], "idea": i["title"], "why": i.get("why", ""), "pillar": i.get("pillar", "")} for i in batch], ensure_ascii=False)}
Return JSON: {{"items": [{{"key": str, "promise": str, "why_click": str, "titles": [str], "thumbnails": [{{"visual": str, "text": str, "emotion": str}}]}}]}}"""
        try:
            res = oe.ai_json(cfg, prompt)
            for it in res.get("items", []):
                db.execute("INSERT OR REPLACE INTO ai_notes VALUES(?,?,?,?)", ("package", str(it.get("key")), json.dumps(it, ensure_ascii=False), today))
        except Exception as e:
            H.log(f"Idea packaging failed: {str(e)[:120]}")
    db.commit()


def notes(db, kind):
    return {r["key"]: json.loads(r["data"]) for r in db.execute("SELECT key, data FROM ai_notes WHERE kind=?", (kind,))}


# --------------------------------------------------------------------------- idea bank
def refresh_ideas(cfg, db, today, helpers):
    """Called after the Sunday opportunity run so the Idea bank picks up the new Top 10."""
    global H
    H = oe.H = helpers
    ensure_schema(db)
    build_ideas(cfg, db, today)


def build_ideas(cfg, db, today):
    data = dashboard_data(cfg, db, today, with_ideas=False)
    ideas = []
    opp = oe.dashboard_data(db)
    if opp:
        for o in [x for x in opp["items"] if x.get("in_top10")]:
            r = (o.get("deep") or {}).get("recommendation") or {}
            ideas.append({"id": "opp:" + o["video_id"], "source": "Weekly opportunity", "title": r.get("title") or o["title"],
                          "pillar": o["cluster"], "why": r.get("why_worth_it") or o.get("viewer_problem", ""), "score": o["score"],
                          "evidence": f'{o["channel"]} ({o["size"]}): {o["cpm"] or "?"}× channel average, {o["vs_ratio"] or "?"} views per subscriber',
                          "link": o["url"]})
    for v in data["mine"]["outliers"][:4] if data["mine"] else []:
        for n, s in enumerate((notes(db, "sequel").get(v["id"]) or {}).get("ideas", [])[:3]):
            ideas.append({"id": f'sequel:{v["id"]}:{n}', "source": "Sequel to your outlier", "title": s.get("title", ""), "pillar": "",
                          "why": s.get("why", ""), "score": min(95, 60 + 5 * v["multiple"]), "evidence": f'Your video did {v["multiple"]}× your median', "link": v["url"]})
    rn = notes(db, "remake")
    for v in data["remakes"][:10]:
        a = rn.get(v["id"], {})
        ideas.append({"id": "remake:" + v["id"], "source": "Remake", "title": a.get("title") or v["title"], "pillar": v["pillar"],
                      "why": a.get("angle") or "Evergreen demand: still pulling views years later", "score": v["remake_score"],
                      "evidence": f'{H_fmt(v["views"])} views, {v["age_years"]} years old, {v["channel"]} ({v["size"]})', "link": v["url"]})
    for g in data["gaps"][:8]:
        ideas.append({"id": "gap:" + g["phrase"], "source": "Content gap", "title": g["phrase"], "pillar": "",
                      "why": "People search this and the current results are weak or old", "score": g.get("gap_score", 0),
                      "evidence": f'median top result {H_fmt(g.get("median_views"))} views, {round((g.get("median_age_days") or 0) / 365, 1)} years old, {g.get("small_channel_winners", 0)} small channels winning'})
    fn = notes(db, "format")
    for v in data["scout"][:6]:
        for n, t in enumerate((fn.get(v["id"]) or {}).get("adaptations", [])[:2]):
            ideas.append({"id": f'format:{v["id"]}:{n}', "source": "Format from another niche", "title": t, "pillar": "",
                          "why": f'Formula: {(fn.get(v["id"]) or {}).get("formula", "")}', "score": 55 + min(20, 5 * (v["vs_ratio"] or 0)),
                          "evidence": f'"{v["title"]}" — {v["vs_ratio"]} views per subscriber', "link": v["url"]})
    for c in data["calendar"][:3]:
        ideas.append({"id": f'season:{c["name"]}:{c["date"]}', "source": "Season", "title": c["name"] + ": " + c["angle"], "pillar": ", ".join(c.get("pillars", [])),
                      "why": f'Publish by {c["publish_by"]}', "score": 50, "evidence": f'Moment on {c["date"]}'})
    ideas = [i for i in ideas if i["title"]]
    ideas.sort(key=lambda i: i["score"] or 0, reverse=True)
    if oe.ai_available():
        pk = notes(db, "package")
        for i in ideas:
            if i["id"] in pk:
                i["titles"] = pk[i["id"]].get("titles")
        package_ideas(cfg, db, today, ideas)
    pk = notes(db, "package")
    for i in ideas:
        p = pk.get(i["id"])
        if p:
            i.update(promise=p.get("promise"), why_click=p.get("why_click"), titles=p.get("titles", []), thumbnails=p.get("thumbnails", []))
        old = db.execute("SELECT first_day FROM ideas WHERE id=?", (i["id"],)).fetchone()
        db.execute("INSERT OR REPLACE INTO ideas VALUES(?,?,?,?)", (i["id"], json.dumps(i, ensure_ascii=False), today, old["first_day"] if old else today))
    db.commit()


def H_fmt(n):
    if n is None:
        return "?"
    n = float(n)
    for d, s in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= d:
            return f"{n / d:.1f}{s}"
    return str(int(n))


# --------------------------------------------------------------------------- calendar
def calendar(cfg, today):
    t = dt.date.fromisoformat(today)
    out = []
    for ev in cfg.get("builder", {}).get("calendar", []):
        dates = []
        if ev.get("monthly"):
            for k in range(0, 3):
                m = (t.month - 1 + k) % 12 + 1
                y = t.year + (t.month - 1 + k) // 12
                dates.append(dt.date(y, m, 1))
        else:
            for y in (t.year, t.year + 1):
                try:
                    dates.append(dt.date(y, ev["month"], ev["day"]))
                except ValueError:
                    pass
        for d in dates:
            if 0 <= (d - t).days <= cfg.get("builder", {}).get("calendar_horizon_days", 75):
                out.append({"name": ev["name"], "date": d.isoformat(), "days_away": (d - t).days, "angle": ev.get("angle", ""),
                            "pillars": ev.get("pillars", []), "start_by": (d - dt.timedelta(days=ev.get("lead_days", 14))).isoformat(),
                            "publish_by": (d - dt.timedelta(days=ev.get("publish_days_before", 3))).isoformat()})
    out.sort(key=lambda e: e["date"])
    return out


# --------------------------------------------------------------------------- dashboard data
def dashboard_data(cfg, db, today, with_ideas=True):
    try:
        ensure_schema(db)
    except Exception:
        pass
    now = dt.datetime.now(dt.timezone.utc)

    def pd(s):
        try:
            return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        except Exception:
            return None

    regexes = oe.cluster_regexes(cfg)
    my_id = cfg.get("my_channel", {}).get("channel_id")

    # ---- my channel
    mine = None
    day = db.execute("SELECT MAX(day) FROM my_video_stats").fetchone()[0]
    if day:
        rows = db.execute("SELECT * FROM my_video_stats WHERE day=?", (day,)).fetchall()
        week = (dt.date.fromisoformat(day) - dt.timedelta(days=7)).isoformat()
        old = {r["video_id"]: r["views"] for r in db.execute("""
            SELECT s.video_id, s.views FROM my_video_stats s JOIN (SELECT video_id, MAX(day) d FROM my_video_stats WHERE day<=? GROUP BY video_id) m
            ON m.video_id=s.video_id AND m.d=s.day""", (week,))}
        vids = []
        for r in rows:
            pub = pd(r["published"])
            vids.append({"id": r["video_id"], "title": r["title"], "url": f"https://www.youtube.com/watch?v={r['video_id']}",
                         "thumb": f"https://i.ytimg.com/vi/{r['video_id']}/mqdefault.jpg", "published": (r["published"] or "")[:10],
                         "age_days": (now - pub).days if pub else None, "duration_s": r["duration_s"], "long": (r["duration_s"] or 0) >= 300,
                         "views": r["views"], "likes": r["likes"], "comments": r["comments"],
                         "views_7d": (r["views"] - old[r["video_id"]]) if r["video_id"] in old else None,
                         "formats": title_formats(r["title"])})
        vids.sort(key=lambda v: v["published"], reverse=True)
        mature_long = [v for v in vids if v["long"] and (v["age_days"] or 0) >= 7][:10]
        med = statistics.median([v["views"] for v in mature_long]) if len(mature_long) >= 3 else None
        for v in vids:
            v["multiple"] = round(v["views"] / med, 2) if med and v["long"] else None
        pub_ch = db.execute("SELECT data FROM studio WHERE kind='channel_public' ORDER BY day DESC LIMIT 1").fetchone()
        sq = notes(db, "sequel")
        outliers = sorted([v for v in vids if (v["multiple"] or 0) >= 2], key=lambda v: v["multiple"], reverse=True)
        for v in outliers:
            v["sequels"] = (sq.get(v["id"]) or {}).get("ideas", [])
        mine = {"day": day, "channel": json.loads(pub_ch["data"]) if pub_ch else None, "median_long_views": med,
                "videos": vids, "outliers": outliers}

    # ---- studio
    studio = None
    sday = db.execute("SELECT MAX(day) FROM studio WHERE kind='videos90'").fetchone()[0]
    if sday:
        studio = {"day": sday}
        for r in db.execute("SELECT kind, data FROM studio WHERE day=? AND kind<>'channel_public'", (sday,)):
            studio[r["kind"]] = json.loads(r["data"])
        titles = {v["id"]: v["title"] for v in (mine or {}).get("videos", [])}
        for row in studio.get("videos90", []) if isinstance(studio.get("videos90"), list) else []:
            row["title"] = titles.get(row.get("video"), row.get("video"))
        for row in studio.get("impressions90", []) if isinstance(studio.get("impressions90"), list) else []:
            row["title"] = titles.get(row.get("video"), row.get("video"))

    # ---- remakes
    remakes = []
    remake_notes = notes(db, "remake")
    for r in db.execute("""SELECT v.*, s.views, s.likes, s.comments, c.title ctitle, c.subs, rm.query
                           FROM remakes rm JOIN videos v ON v.id=rm.video_id
                           JOIN video_stats s ON s.video_id=v.id AND s.day=(SELECT MAX(day) FROM video_stats WHERE video_id=v.id)
                           LEFT JOIN channels c ON c.id=v.channel_id"""):
        pub = pd(r["published_at"])
        if not pub or not oe.eligibility(r, cfg, my_id)[0]:
            continue
        topic = oe.classify_topic(r["title"] or "", json.loads(r["tags"] or "[]"), r["description"] or "", regexes)
        if not topic:
            continue
        age_days = max((now - pub).days, 1)
        first = db.execute("SELECT day, views FROM video_stats WHERE video_id=? ORDER BY day LIMIT 1", (r["id"],)).fetchone()
        span = (dt.date.fromisoformat(db.execute("SELECT MAX(day) FROM video_stats WHERE video_id=?", (r["id"],)).fetchone()[0]) - dt.date.fromisoformat(first["day"])).days
        current_vpd = round((r["views"] - first["views"]) / span) if span >= 3 else None
        lifetime_vpd = r["views"] / age_days
        vs = r["views"] / r["subs"] if r["subs"] else None
        size = oe.size_class(r["subs"])
        demand = max(0, min(35, (math.log10(max(r["views"], 1)) - 4) / 3 * 35))
        pull = 10 if current_vpd is None else 25 if current_vpd >= 0.5 * lifetime_vpd else 15 if current_vpd >= 0.2 * lifetime_vpd else 5
        outl = 0 if vs is None else 20 if vs > 3 else 14 if vs >= 1 else 8 if vs >= 0.5 else 3
        stale = 10 if age_days >= 1095 else 7 if age_days >= 730 else 5 if age_days >= 365 else 2
        small = {"Micro": 10, "Small": 10, "Medium": 6, "Large": 3}.get(size, 0)
        remakes.append({"id": r["id"], "title": r["title"], "url": f"https://www.youtube.com/watch?v={r['id']}",
                        "thumb": f"https://i.ytimg.com/vi/{r['id']}/mqdefault.jpg", "channel": r["ctitle"], "size": size, "subs": r["subs"],
                        "views": r["views"], "age_years": round(age_days / 365, 1), "vs_ratio": round(vs, 2) if vs else None,
                        "current_vpd": current_vpd, "lifetime_vpd": round(lifetime_vpd), "pillar": topic[0], "query": r["query"],
                        "remake_score": round(demand + pull + outl + stale + small),
                        "angle": remake_notes.get(r["id"]) or {}})
    remakes.sort(key=lambda v: v["remake_score"], reverse=True)
    remakes = remakes[:60]

    # ---- gaps
    gaps = [json.loads(r["data"]) | {"checked": r["day"]} for r in db.execute("SELECT data, day FROM gaps")]
    gaps = [g for g in gaps if g.get("results")]
    gaps.sort(key=lambda g: g.get("gap_score", 0), reverse=True)

    # ---- format scout (neighbouring niches) + format lift inside your niche
    fn = notes(db, "format")
    scout = []
    cut = (dt.date.fromisoformat(today) - dt.timedelta(days=21)).isoformat()
    for r in db.execute("""SELECT v.*, s.views, c.subs, c.title ctitle, sc.query FROM scout sc JOIN videos v ON v.id=sc.video_id
                           JOIN video_stats s ON s.video_id=v.id AND s.day=(SELECT MAX(day) FROM video_stats WHERE video_id=v.id)
                           LEFT JOIN channels c ON c.id=v.channel_id WHERE sc.day>=?""", (cut,)):
        if not r["subs"] or (r["duration_s"] or 0) < 300:
            continue
        vs = r["views"] / r["subs"]
        size = oe.size_class(r["subs"])
        if vs >= 2 or (vs >= 1 and size in ("Micro", "Small", "Medium")):
            scout.append({"id": r["id"], "title": r["title"], "url": f"https://www.youtube.com/watch?v={r['id']}",
                          "thumb": f"https://i.ytimg.com/vi/{r['id']}/mqdefault.jpg", "channel": r["ctitle"], "size": size,
                          "views": r["views"], "vs_ratio": round(vs, 2), "query": r["query"], "formats": title_formats(r["title"]),
                          "ai": fn.get(r["id"]) or {}})
    scout.sort(key=lambda v: v["vs_ratio"], reverse=True)
    scout = scout[:40]

    lift = []
    month = (now - dt.timedelta(days=60)).isoformat()
    pool = db.execute("""SELECT v.title, s.views, c.subs FROM videos v
                         JOIN video_stats s ON s.video_id=v.id AND s.day=(SELECT MAX(day) FROM video_stats WHERE video_id=v.id)
                         LEFT JOIN channels c ON c.id=v.channel_id WHERE v.published_at>=? AND v.duration_s>=300 AND c.subs>0""", (month,)).fetchall()
    if len(pool) >= 40:
        allc, outc = defaultdict(int), defaultdict(int)
        outs = [p for p in pool if p["views"] / p["subs"] >= 1]
        for p in pool:
            for f in title_formats(p["title"]):
                allc[f] += 1
        for p in outs:
            for f in title_formats(p["title"]):
                outc[f] += 1
        for f, n in allc.items():
            if n >= 5 and outs:
                share_all, share_out = n / len(pool), outc[f] / len(outs)
                lift.append({"format": f, "videos": n, "outliers": outc[f], "lift": round(share_out / share_all, 2) if share_all else None,
                             "examples": [p["title"] for p in outs if f in title_formats(p["title"])][:3]})
        lift.sort(key=lambda x: x["lift"] or 0, reverse=True)

    out = {"mine": mine, "studio": studio, "studio_connected": bool(sday), "remakes": remakes, "gaps": gaps[:60],
           "scout": scout, "format_lift": lift, "calendar": calendar(cfg, today),
           "builder_run": None}
    br = db.execute("SELECT day, summary FROM builder_runs ORDER BY day DESC LIMIT 1").fetchone()
    if br:
        out["builder_run"] = json.loads(br["summary"])
    if with_ideas:
        iday = db.execute("SELECT MAX(day) FROM ideas").fetchone()[0]
        out["ideas"] = [json.loads(r["data"]) | {"first_day": r["first_day"]} for r in db.execute("SELECT data, first_day FROM ideas WHERE day=?", (iday,))] if iday else []
        out["ideas"].sort(key=lambda i: i.get("score") or 0, reverse=True)
    return out
