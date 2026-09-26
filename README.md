# TN news monitor

Collects Tamil and English news from Google News and YouTube every 30 minutes on
GitHub, scores it against a keyword sheet, and (step 3) sends a Telegram digest
twice a day.

## Workflows (Actions tab)

| Workflow | When | What |
|---|---|---|
| 1 - GitHub test run | by hand | checks that GitHub's servers can reach the feeds and sites; stores nothing |
| 2 - Collect news | every 30 min | restores the database, collects and scores, saves the database again |

The database travels between runs as an encrypted run artifact (`state`, kept 3
days; `state-backup`, once a day, kept 30 days). Instant alerts are switched off
(`TELEGRAM_DRY_RUN=1`); nothing reaches Telegram until the digest is built.

## Secrets (Settings → Secrets and variables → Actions)

| Secret | Needed | What |
|---|---|---|
| `STATE_PASSPHRASE` | yes | any long passphrase; encrypts the saved database. Changing it orphans the saved copy |
| `KEYWORDS_CSV_URL` | yes | the keyword Google Sheet, published to the web as CSV |
| `YOUTUBE_API_KEY` | recommended | YouTube Data API v3 key; the RSS feed often fails from cloud servers |
| `GEMINI_API_KEY` | step 3 | Gemini free tier, for the digest |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | step 3 | the bot and the private channel |

## Keeping the keyword list

`keywords_seed.csv` is deliberately **not** in this public repository. The live
list is the Google Sheet; the collector caches a copy inside the database, so a
sheet outage does not stop scoring. Edit terms in the sheet; changes reach the
collector within about 10 minutes.

## Never upload these files

- `.env` (keys and tokens; on GitHub they go in Secrets)
- `corpus.db`, `backtest.db` and any other `.db` file
- `keywords_seed.csv`, `label_me.csv`, `dupe_pairs.csv`, `ai_cache.json`
- `collect.log` and any other log

## On your own PC

Everything still runs locally as before (`python collect.py --init`, then
`python collect.py`), with `keywords_seed.csv` next to the code or
`KEYWORDS_CSV_URL` in `.env`. Run `python test_phase1.py` after any change.
