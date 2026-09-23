# TN news monitor

Collects Tamil and English news from Google News and YouTube, and will send a
Telegram digest twice a day.

**Current stage: step 1, a one-off test.** Open the **Actions** tab, choose
**1 - GitHub test run**, and press **Run workflow**. The result appears on the
run's Summary page. The test stores nothing.

## Never upload these files

This repository is public. Keep the following on your own computer only:

- `.env` (keys and tokens; on GitHub they go in Settings → Secrets)
- `corpus.db`, `backtest.db` and any other `.db` file
- `keywords_seed.csv` (the keyword list lives in the Google Sheet)
- `label_me.csv`, `dupe_pairs.csv`, `ai_cache.json`
- `collect.log` and any other log
