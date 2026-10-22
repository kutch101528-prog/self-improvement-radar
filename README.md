# Self-Improvement Trend Radar

A daily tracker for the self-improvement / personal-development niche on YouTube, plus the blogs, newsletters, Reddit and news around it. Each run saves a snapshot, so the dashboard can show growth since yesterday, days since upload, days on your radar, rising keywords and channel growth.

## What it collects every day

- **Keyword searches**: every keyword in `config.json`, videos from the last 7 days sorted by views.
- **Trending charts**: YouTube's Education, Howto & Style and People & Blogs charts in US, IN, GB, filtered to the niche.
- **Channels, small to large**: your seed list plus channels discovered automatically when one of their videos gets 3× or more their normal views. Their new uploads are then checked every day.
- **Search suggestions**: what YouTube autocompletes for your seed phrases; new phrases are highlighted.
- **Blogs, newsletters, news, Reddit**: RSS feeds listed in `config.json`.
- **Intros (optional)**: the first 60 seconds of transcript for the day's top 25 videos, plus the description opening for every video.

## Weekly long-form opportunities (every Sunday)

Built around your fixed brief for @Kutchified: practical, organized, actionable self-improvement.

- **Fixed niche:** the 13 pillars and their full keyword maps live in `config.json` under `clusters`. Each pillar has 10 searches, one per phrase type (core, how-to, problem, transformation, mistakes, list, system, challenge, contrarian, current audience language). Each search runs with YouTube's 4–20 min and 20+ min filters, over the last 21 and 100 days, so Shorts are never requested. The daily runs rotate through all 520 combinations about once a week.
- **Long-form only:** videos must be 5+ minutes, English, 1–100 days old, and not podcasts, interviews, livestreams, music, timers, speeches, compilations, reactions, sermons, kids content, English lessons, or home-storage/desk/hardware videos without a productivity system. Videos over 40 minutes need exceptional evidence. Your own videos are excluded.
- **Channel baseline:** Channel Performance Multiple = views ÷ median views of the channel's previous 10 comparable long-form videos (at least 5 required; otherwise Unavailable).
- **Qualification:** at least 3 of the brief's conditions, including at least one real performance signal.
- **Fixed 100-point score:** VPH and momentum 25, channel outlier strength 20, views ÷ subscribers 15, alignment 15, packaging 10, replicability 10, recency 5, minus the brief's penalties. Produce Now only passes if every hard requirement is met.
- **Trends:** Single-Video Signal, Emerging, Validated, Evergreen, Saturated, judged across unrelated channels.
- **Weekly history:** every tracked video gets a permanent weekly snapshot and a status (New, Rising, Holding, Falling, Dropped, Recovered).
- **Deep research:** the Top 10, rising videos and exceptional small-channel outliers get the full analysis (why it works, title, thumbnail, hook, engagement and shareability, replication) plus an original video plan for your channel.
- **Where it lives:** the dashboard's **Weekly opportunities** tab replaces the Notion databases, with the same views (Produce Now, Top 10, Rising, Micro-Channel Breakouts, each pillar, Skip, and more). Each week's report is saved at `/reports/YYYY-MM-DD.html`.
- **Email:** the Best 5 plus "the video to make this week", sent through Gmail. Needs three GitHub secrets: `SMTP_USER` (your Gmail address), `SMTP_APP_PASSWORD` (a Google app password, not your normal password) and `REPORT_EMAIL_TO` (where to send it).

Every number is labelled Verified (straight from YouTube), Calculated, Inferred (AI or rules) or Unavailable. vidIQ scores are always Unavailable because vidIQ has no public API. Run it on any day with `python tracker.py --weekly`.

## Channel builder (your topic system)

**Daily:** your own uploads and growth, and your outliers (long-form videos at 2× or more your median views). If Studio is connected, also retention, the searches that bring people to you, traffic sources, and impressions/click-through when the API provides them.

**Every Wednesday:**
- **Remake finder:** evergreen outliers 6 months to 5 years old in your pillars, scored on demand, whether they still pull views, views per subscriber, age and channel size.
- **Content gaps:** real autocomplete searches checked against what ranks today. A high score means demand exists but the results are old, Shorts, or small channels winning.
- **Title formats that win:** which title structures appear more among outliers than among all long-form videos in your niche (last 60 days).
- **Formats from other niches:** titles breaking out in fitness, money, study and similar niches, with the reusable formula and adaptations for your pillars (AI).
- **Sequels** to your own outliers, **remake angles**, and **10 titles + 3 thumbnail concepts** for each top idea (AI).
- **Season calendar** with start-by and publish-by dates (edit it in `config.json` → `builder` → `calendar`).

**Idea bank:** every idea from every source in one ranked list. For each idea, pick a title, count votes from a quick "which would you click?" test, write notes, and move it through Idea → Packaging → Testing → Scripting → Filming → Editing → Published. Progress is saved in your browser; use **Back up progress** now and then.

Run it on any day with `python tracker.py --builder`.

### Connect your Studio data (optional, about 15 minutes, once)

1. In Google Cloud Console (same **trend-radar** project), go to **APIs & Services → Library**, search for **YouTube Analytics API** and click **Enable**.
2. Go to **APIs & Services → OAuth consent screen**:
   - choose **External**, fill in an app name (e.g. "trend radar") and your email
   - on the Scopes step, add `.../auth/yt-analytics.readonly`
   - add your own Google account as a test user
   - then click **Publish app** so access doesn't expire every 7 days. Google may show "unverified app" later; that's expected for a personal tool.
3. Go to **Credentials → Create credentials → OAuth client ID**, choose application type **Desktop app**, and click **Download JSON**.
4. On your own computer, run `python3 get_youtube_token.py path/to/the-downloaded-file.json`. Sign in with the account that owns your channel. If you see "Google hasn't verified this app", click Advanced → continue, then click Allow.
5. Add the three printed values as GitHub secrets: `YT_OAUTH_CLIENT_ID`, `YT_OAUTH_CLIENT_SECRET` and `YT_OAUTH_REFRESH_TOKEN`. Then delete the downloaded JSON file.

Access is read-only. You can revoke it any time at myaccount.google.com/permissions.

## Your weekly playbook

- **Sunday (15 min):** read the email. Open **Weekly opportunities**, then **Idea bank**, and choose the week's video. Default to the "video to make this week" unless one of your own outlier sequels scores higher.
- **Monday (45 min):** packaging first. Pick 3 favourite titles and sketch 2 thumbnails. Show them to 5 people from your audience and count votes with +1. If nothing wins clearly, go back to the Idea bank.
- **Tuesday to Friday:** script (promise in the first 15 seconds, deliver fast, over-deliver), film and edit.
- **Wednesday (10 min):** look at **Topic finder** and add the best remakes, gaps and borrowed formats to your shortlist for next week.
- **48 hours after publishing:** check click-through and retention in Studio. Compare with the source video's packaging. A winner becomes a sequel; a miss tells you whether the topic, the packaging or the delivery failed.

## Idea-building features

- **Save button** on every video card, with a Saved tab and CSV export. Saved videos live in your browser only (not shared, not uploaded) and stay even after they leave the radar.
- **Audience tab**: the most-liked comments on the day's 40 biggest breakout videos, with a "Questions only" filter. Comment text and like counts are stored; commenters' names are not.
- **Intros**: the first 60 seconds of transcript for the day's top 25 videos. YouTube sometimes blocks transcript requests from cloud servers such as GitHub's; when that happens the run log says "No transcript" and the description hook is shown instead.
- **Weekly insights tab**: every Monday, an AI write-up of what the week's breakouts have in common (topics, title patterns, length, viewer questions, 5 ideas). Needs one extra GitHub secret:
  - `GEMINI_API_KEY` (free tier, from aistudio.google.com), or
  - `ANTHROPIC_API_KEY` (paid, from console.anthropic.com; costs a few cents per write-up).
  Only public video titles, stats, keywords and comment text are sent. Run `python tracker.py --insights` to make one immediately.

## Dashboard

- Videos: thumbnail grid with title, channel size tier, views, days since upload, views/day, outlier score (views ÷ channel's average), growth since the last check, days on radar, engagement, intro. Filter by upload window, channel size, Shorts vs long-form, outlier, "new today"; export CSV.
- Keywords: title words/phrases rising vs the 7-day average; select one to jump to its videos.
- Channels: subscribers, 7-day subscriber change, average views, best outlier, followed or auto-discovered.
- Blogs & newsletters, Reddit.

Open `dashboard.html` in a browser. Each day's copy is also kept in `data/archive/YYYY-MM-DD.html`.

## Setup (10 minutes)

1. Python 3.10+ (no packages required).
2. Get a YouTube Data API key: Google Cloud Console → new project → enable **YouTube Data API v3** → Credentials → Create API key. Then **restrict the key** to YouTube Data API v3 only.
3. Set the key as an environment variable (never put it in the code or config):
   - macOS/Linux: `export YT_API_KEY="your-key"`
   - Windows PowerShell: `$env:YT_API_KEY="your-key"`
4. Try the interface with sample data: `python tracker.py --demo`, then open `dashboard_demo.html`.
5. Real run: `python tracker.py`

Other options: `--skip-youtube` (feeds and suggestions only), `--rebuild` (redraw from stored data, no network).

## Run it every day

- **Mac/Linux (cron)**: `crontab -e`, then
  `30 7 * * * cd /path/to/yt-self-improvement-radar && YT_API_KEY=... /usr/bin/python3 tracker.py >> data/run.log 2>&1`
  (better: load the key from a file only you can read, e.g. `chmod 600`.)
- **Windows**: Task Scheduler → daily trigger → `python tracker.py` with "Start in" set to this folder.
- **GitHub Actions**: `.github/workflows/daily.yml` is included. Use a **private** repo and add `YT_API_KEY` under Settings → Secrets and variables → Actions. Enable MFA on the GitHub account.

## Quota (important)

The free quota is 10,000 units/day. A keyword search costs 100 units; almost everything else costs 1. With the default 84 keywords the run uses roughly 9,000 units. If the budget runs out, the keywords that were skipped go first the next day, so nothing is permanently missed. To cover more keywords, request a quota increase in Google Cloud Console, or raise `search_window_days` and search fewer keywords.

## Notes

- Seed channel handles and feed URLs are a starting point. Anything that fails is shown in the dashboard's run log; fix or remove it in `config.json`.
- The search-suggestion endpoint and Reddit RSS are unofficial/best-effort and may change or rate-limit.
- Intros need `youtube-transcript-api` (see `requirements-optional.txt`). It's a third-party package that reads YouTube's transcript pages; check it against your approved package list, pin a version, and keep `intros_per_day` low to avoid being blocked.
- Shorts are approximated as videos 3 minutes or shorter.
- Security: all external text is rendered as plain text (no HTML injection), only http(s) links are clickable, CSV export guards against spreadsheet formula injection, database writes use parameterised queries, and the API key is never written to logs or errors.
