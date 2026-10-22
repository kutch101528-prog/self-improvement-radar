# Self-Improvement Trend Radar

A daily tracker for the self-improvement / personal-development niche on YouTube, plus the blogs, newsletters, Reddit and news around it. Each run saves a snapshot, so the dashboard can show growth since yesterday, days since upload, days on your radar, rising keywords and channel growth.

## What it collects every day

- **Keyword searches**: every keyword in `config.json`, videos from the last 7 days sorted by views.
- **Trending charts**: YouTube's Education, Howto & Style and People & Blogs charts in US, IN, GB, filtered to the niche.
- **Channels, small to large**: your seed list plus channels discovered automatically when one of their videos gets 3× or more their normal views. Their new uploads are then checked every day.
- **Search suggestions**: what YouTube autocompletes for your seed phrases; new phrases are highlighted.
- **Blogs, newsletters, news, Reddit**: RSS feeds listed in `config.json`.
- **Intros (optional)**: the first 60 seconds of transcript for the day's top 25 videos, plus the description opening for every video.

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
