# TN news monitor

Collects Tamil and English news from Google News and YouTube every 30 minutes on
GitHub, scores it against a keyword sheet, and (step 3) sends a Telegram digest
twice a day.

## Workflows (Actions tab)

| Workflow | When | What |
|---|---|---|
| 1 - GitHub test run | by hand | checks that GitHub's servers can reach the feeds and sites; stores nothing |
| 2 - Collect news | every 30 min | restores the database, collects and scores, saves the database again |
| 3 - Telegram brief | ~07:00 and ~18:00 IST | ranks what was collected since the last brief (Gemini, or the keyword rules as fallback), posts up to 30 items to Telegram, and publishes the same brief as a web page in `docs/` |

## The web page

Each brief is also written to `docs/index.html` (latest) and `docs/briefs/<date>-<slot>.html`
(last 30) and committed to `main`. Turn on GitHub Pages once: Settings → Pages → Source
"Deploy from a branch", branch `main`, folder `/docs`. The page is public to anyone with
the address (it carries `noindex`, so search engines are asked not to list it). A custom
domain can be set on the same settings page.

The database travels between runs as an encrypted run artifact (`state`, kept 3
days; `state-backup`, once a day, kept 30 days). Instant alerts are switched off
(`TELEGRAM_DRY_RUN=1`); Telegram only receives the twice-daily brief.

## Secrets (Settings → Secrets and variables → Actions)

| Secret | Needed | What |
|---|---|---|
| `STATE_PASSPHRASE` | yes | any long passphrase; encrypts the saved database. Changing it orphans the saved copy |
| `KEYWORDS_CSV_URL` | yes | the keyword Google Sheet, published to the web as CSV |
| `YOUTUBE_API_KEY` | recommended | YouTube Data API v3 key; the RSS feed often fails from cloud servers |
| `YOUTUBE_SHEET_CSV_URL` | optional | the sheet's *YouTube channels* tab, published as CSV (columns `name`, `channel_id`, `enabled`). When set, the channel list comes from there instead of `sources.json` |
| `GEMINI_API_KEY` | recommended | Gemini free tier; ranks the brief. Without it the keyword rules rank |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | yes, for the brief | the bot and the private channel. Without them the brief is only printed on the run page |

Optional *variables* (Settings → Secrets and variables → Actions → Variables): `DIGEST_PER_CATEGORY` (default 5), `DIGEST_MAX` (default 30), `GEMINI_MODEL` (default `gemini-flash-lite-latest`).

## Keeping the keyword list

`keywords_seed.csv` is deliberately **not** in this public repository. The live
list is the Google Sheet; the collector caches a copy inside the database, so a
sheet outage does not stop scoring. Edit terms in the sheet; changes reach the
collector within about 10 minutes. Every run starts with `collect.py --check-keywords`,
which stops the run with a plain message if the sheet link does not return CSV.

## Never upload these files

- `.env` (keys and tokens; on GitHub they go in Secrets)
- `corpus.db`, `backtest.db` and any other `.db` file
- `keywords_seed.csv`, `label_me.csv`, `dupe_pairs.csv`, `ai_cache.json`
- `collect.log` and any other log

## On your own PC

Everything still runs locally as before (`python collect.py --init`, then
`python collect.py`), with `keywords_seed.csv` next to the code or
`KEYWORDS_CSV_URL` in `.env`. Run `python test_phase1.py` after any change.
