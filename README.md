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

The Telegram brief carries only what is new since the last brief. The web page is different:
`docs/index.html` is a small app that renders editions from `docs/data/`: `latest.json`
(everything kept in the last 24 hours, up to 60 stories) and one file per IST day by
publication date (kept **forever**; the picker offers 7 and the ☰ menu's *Archive* lists every
day by month, with an "all kept" list per day from `docs/archive/`). Each edition has a 60-second briefing with 2-3
takeaways per section (tap a takeaway to jump to its card, which lights up; tap a section heading for the whole section), a category × sentiment dashboard for the day that filters the cards when tapped, a 7-day
matrix (days × sections with sentiment bars and 🟢 ⚪ 🔴 counts, the whole day in the *All* column; tap a count for that day and section, a badge for one sentiment of it, the day for all of it), expandable
"+N more outlets" links on merged stories, two AI notes on every card (📝 Notes toggle hides them for a compact view), a ☑ Select mode that bundles chosen stories into one numbered WhatsApp message (with or without the notes, also for the per-card 📲 share),
search (headlines, summaries, outlets), cards/list, a slide-out menu, back-to-top, WhatsApp share per card, and reader
feedback (👍/👎 and "Submit missing news") when `FEEDBACK_URL` is set — see `feedback.gs`.
Every run rebuilds `latest`, today and yesterday. `python digest.py --backfill 7` (or the
workflow's *backfill_days* input) writes the past days from what the database holds.
All of it is committed to `main` after each real brief. Turn on GitHub Pages once: Settings → Pages → Source
"Deploy from a branch", branch `main`, folder `/docs`. The page is public to anyone with
the address (it carries `noindex`, so search engines are asked not to list it). A custom
domain can be set on the same settings page.

## What Gemini may not decide alone

Every verdict -- live, cached or from the keyword rules -- passes four guards in `digest.py`
before it reaches a page or the brief; a rejected verdict is re-filed by the keyword rules
(constituency / district / portfolio / political when their own evidence is in the article),
else `none`. Each correction is counted on the run page and the status page.

- **Mentions** only when the article literally names him (`MINISTER_NAMES`). Minister
  Rajkumar, Minister Rajeev, Ramesh Kumar and every other Kumar are not him. A note or
  briefing line that puts his name into an article that does not carry it is thrown away
  and written again with the article marked.
- **Velachery** only for AC-26: a headline naming Velachery, Taramani, Adyar, Besant Nagar,
  Thiruvanmiyur, Pallikaranai or Adambakkam (or their colonies) stays; a headline naming
  Ambattur, Thoraipakkam, Sholinganallur, Perungudi, Tambaram, Chromepet, Pallavaram, GST
  Road, Guindy, Saidapet, Porur or Medavakkam is re-filed (Ambattur to Thiruvallur); a
  headline naming neither needs an AC-26 locality in the text or the sheet's constituency tag.
- **Political** only with one of his anchors in the article (his name, an AC-26 locality, a
  Thiruvallur-district place, his portfolio); by-election celebrations elsewhere are `none`.
- **Commercial property** marketing (office-space or rent guides, flats for sale, 2BHK,
  price per sq ft) is `none` -- unless the article has a governance term (Collector,
  Corporation, encroachment, acquisition, demolition, lake, patta, poramboke, CMDA ...),
  which bypasses the filter entirely.

## How reports become one card

Reports of one event are merged so a story appears once with its other outlets behind
"+N more outlets". Gemini groups the edition's items and must name the entities each group
shares; a group whose names do not occur in a member is thinned or refused. Within one
category a proven group is one card, however many outlets report it (ten reports of a Metro
fare are one card: the fullest, latest report in front, up to 8 distinct outlets behind it,
the rest absorbed). Across categories, and for Gemini's batch story numbers and near-identical
headlines, the two reports must also share an identifying headline word (a name, a place, an
organisation -- never a number or a date), and a Tamil/English pair must be in one category.
Links never chain. An outlet's update on its own earlier report joins the same card, and so
does a Facebook or YouTube copy whose post text wraps the headline. Two headlines naming
different places (a Velachery flood, an Adyar flood) are never one story, whoever says so.
When Gemini could not group an edition (budget or time spent), two reports in one category
and one language that share three identifying words and a sixth of their headline -- or two
words and a quarter -- still merge (the MeitY advisory five ways); the run page lists every
Gemini group that was refused or thinned, with the titles, so a missed merge can be traced.
A story whose headline or body is 80% the same as one kept the day before is a re-run and is
left out; a development on the same event (an arrest, an order, a statement) has its own
headline and stays.

## The private status page

`docs/admin.html` is published with the site but linked from nowhere. It shows a PIN prompt
and nothing else: the address of the run logs is sealed inside the page with the PIN
(PBKDF2 + HMAC), so until the right PIN is typed the page makes **no** network request and
names no host. Unlocked, it reads `data/status.json` (written at each brief: Gemini calls
used today of the daily cap and whether it is active, throttled or on standby; the
collector's last poll, items in / kept / dropped over 24 h, work waiting, YouTube channels
returning nothing; groups kept and refused, re-runs dropped, guard corrections; 👍 👎 and
missing-news counts from the Feedback tab) and the live list of recent runs with a link to
each log. Set the `ADMIN_PIN` secret, run a brief once, then open `/admin.html`.

## The archive

Every dated edition stays in `docs/data/` for good (10-40 KB a day). `docs/archive/` adds one
file per day with everything the rules kept that day -- title, link, outlet, category,
sentiment, notes, and whether it made the page; no article bodies -- written when the day is
2 days old and once more at 15 days (Google News re-serves a story for up to 14 days). Four
months is about 15 MB in all. The database itself still keeps 21 days.

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
| `ADMIN_PIN` | optional | unlocks `docs/admin.html`, the private status page (see below). Use a passphrase of 8+ characters, not a 4-digit PIN: the page can be downloaded and guessed at offline |

Optional *variables* (Settings → Secrets and variables → Actions → Variables): `MINISTER_NAMES` (the forms of his name the guards accept, `|`-separated; default `R. Kumar|R Kumar|ஆர். குமார்|ஆர்.குமார்|ஆர் குமார்|அமைச்சர் ஆர். குமார்|அமைச்சர் குமார்|Minister Kumar` -- Tamil case endings are understood; the two generic forms count only next to a Tamil Nadu place or his portfolio), `DIGEST_PER_CATEGORY` (default 5), `DIGEST_MAX` (default 30), `GEMINI_MODEL` (default `gemini-flash-lite-latest`); for the web page `DIGEST_WEB_PER_CATEGORY` (10), `DIGEST_WEB_MAX` (60), `DIGEST_WEB_HOURS` (24), `FEEDBACK_URL` (the Apps Script web-app URL from `feedback.gs`). Secret `FEEDBACK_CSV_URL` (the Feedback tab published as CSV) adds a reader-feedback review block to each brief's run page.

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
