# KRX/NXT operational projection

This extends the existing `kr-market-news-radar` process and repository. It does not register a Sites project or create a Render service. `/` and `/operations` serve the operational dashboard, while `/legacy` preserves the original ALPHA500 dashboard. The existing HTTP APIs, broker sessions, WebSocket subscriptions, collection loops, original decision engines, and their files remain unchanged.

## Input and authority

Each background cycle reads the complete explicit KRX and NXT KIS row responses separately, with `limit=5000` and a checked `total`. Failed live endpoints may fall back to the existing READY row endpoint. Missing venue tags, mixed rows, partial pages, failed requests, unverified timestamps, unknown security types and unknown session calendars never grant entry permission. An HTTP request timestamp does not establish the time of an exchange price.

The operational V1.7 discovery evaluates the *unchanged* native `V17.analyze_one` over every available authority symbol's LS/KRX daily cache in SQLite read-only mode. There is no top-800 limit or remembered watchlist input in this projection. Missing histories are counted as pending; a code with a completed negative evaluation can exit even while other codes lack history. Existing V1.7 outputs remain historical evidence for codes awaiting cache evaluation. No synthetic current-day bars are appended, including on holidays and weekends.

NXT reads the existing native minute series for **every** NXT authority code, independently of the KRX union and the native top-20 shortlist. The native after-session sequence measurements are supplemental LS evidence. No new broker queries, OAuth sessions, WebSocket subscriptions or bar backfills are started. Shared promotion additionally requires 15:30–20:00 KST on a verified trading session. Missing calendar verification remains pending. NXT daily MA references, when sourced from the KRX daily cache, are explicitly labeled as KRX daily references and never become NXT intraday averages.

The asymmetric-RS lane accepts measured RS5/RS20 or an explicit native STRONG/SUPER discovery label. Capital handoff uses traced native rotation discovery with separate explicit handoff confirmation. Neither native rank nor a score grants PRIME. Missing upstream confirmations remain pending.

The 25% lane reconstructs **intraday high / previous close >= 1.25** events from explicit KRX daily history, with the last five cached dates shown. `recent5_complete` requires correspondence to an authoritative trade date and all five histories; the overall universe remains PENDING if even one authority stock lacks complete history. All detected events remain visible in candidate detail. Same-day FAST events cannot become a second-wave PRIME. Dedicated-engine price preservation, supply contraction, compression and support confirmations are not guessed; if the current application does not supply these axes, this lane is WATCH/PENDING.

News classifications come from the existing `NEWS_IMPACT_SIDECAR` using an explicit ticker, published time and original gate state. No news is not interpreted as CLEAR. KIS flows stay authoritative; LS reference flows, when available, are shown separately. Units must be retained upstream; no share-to-currency conversions are invented.

## Promotion contract

PRIME requires **every** mandatory gate to pass: explicit normal/neutral/risk-on market state, clear/confirmed news, NO_CHASE (day return <=5% plus no explicit chase flag), confirmed common share, cap >=700 hundred-million KRW, actual exchange-timestamp freshness <=180 seconds, verified open session, signal freshness <=600 seconds, valid observed entry/stop/target with reward/risk >=2, matching fresh same-venue LS price within 0.5%, at least one entry confirmation and the lane's own confirmations. Unknown is PENDING, not zero or PASS. Thresholds are a conservative operational promotion policy, not a claimed backtested probability or a modification to native engine thresholds.

For NXT, the operational reacceleration confirmation follows the existing A-lane measurements (15-minute volume >=1.5x, turnover >=1.4x, positive 15-minute price, impulse reclaim >=0.98), plus an actual 60-minute history requirement and high-hold >=0.985. It does not generate an extrapolated target. Market vetoes RISK_OFF_ACCEL / PROGRAM_SELL_ACCEL / BREADTH_COLLAPSE and news vetoes PRICED_IN / SELL_THE_NEWS / NEGATIVE_CONFIRMED block entry. Targets and stops without explicit provenance cannot satisfy 2R.

## Records and outcomes

A separate `/data/operations/operations.sqlite3` stores NEW, HOLD, CHANGE and EXIT with original source times and reasons. No 200-record truncation exists. APIs paginate; the UI initially shows 20 candidates and 200 history records. Source failure does not generate a selection exit. An occurrence counts a new selection episode, not every poll. KRX, NXT and each lane have different keys.

PRIME starts an append-only signal observation episode, not a live order. After-signal exchange quotes update sampled return, MFE and MAE. Stops/targets close at the **observed** price; gaps are not filled at a theoretical level. Outcomes are explicitly sampled and exclude fees, slippage and actual order execution. No value, win rate or missed-path extrema are fabricated.

When the existing `ALPHA500_STATE_DATABASE_URL`, `DATABASE_URL` or `POSTGRES_URL` is configured, the adapter uses a new isolated `operations_projection_checkpoint` table to save and restore the SQLite ledger after each cycle. It never changes collector tables. Missing or failed persistence is shown as `PENDING` in the UI; an ephemeral `/data` directory is not claimed to survive a deploy. A successful PostgreSQL checkpoint is required to verify durable deployment history. A checkpoint larger than 32 MB is explicitly flagged for storage migration rather than silently discarded.

## Runtime and validation

- `OPERATIONS_INTERVAL_SECONDS`: minimum 60, default 120.
- `KIS_LEDGER_BASE_URL`: defaults to the established KIS authority.
- Cache resolution follows the existing V1.7 history path configuration.
- `python -m unittest operations.test_engine -q` runs broker-free tests and is also a Docker build step.
- `install.py` fails closed if expected existing handler/server anchors differ.
- The Dockerfile addition is appended; the entire pre-existing Dockerfile prefix is byte-for-byte unchanged.
- Full Docker image build and live service deployment need the confirmed existing Render workspace and actual service configuration; no claim is made from static checks alone.


## Daily-date integrity patch (V1.1)

The operational projection compares the existing LS daily cache to KIS `manifest.trade_date` after that session's close. Until a current five-date daily window and each symbol's actual daily bar are confirmed, KRX daily-dependent lanes remain PENDING and the 25% event window is not presented as a completed scan. Actual historical dates remain visible rather than being replaced by `now`. Bare YYYY-MM-DD values are displayed as daily dates, not converted to 09:00 KST. Daily-bar dates never become synthetic 15:30 signal events. The original KIS/LS collectors and Dockerfile remain byte-identical. If the LS-owned cache is stale, the existing collector must restore history; this read-only projection intentionally does not backfill or create artificial bars.
