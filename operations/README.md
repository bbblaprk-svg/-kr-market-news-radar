# KRX/NXT operational projection

This extends the existing `kr-market-news-radar` process and repository. It does not register a Sites project or create a Render service. `/` and `/operations` serve the operational dashboard, while `/legacy` preserves the original ALPHA500 dashboard. The existing HTTP APIs, broker sessions, WebSocket subscriptions, collection loops, original decision engines, and their files remain unchanged.

## Input and authority

Each background cycle reads the complete KRX and NXT KIS live rows separately, with `limit=5000` and a checked `total`. It fetches `/api/ledger/latest/rows` independently for the READY immutable manifest: verified `round_id`, venue, snapshot start date and optional explicit trade date. A REST live request timestamp never supplies a trade date. Failed live endpoints may fall back to the existing READY row endpoint. Missing venue tags, mixed rows, partial pages, failed requests, unverified timestamps, unknown security types and unknown session calendars never grant entry permission. An HTTP request timestamp does not establish the time of an exchange price.

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

The operational projection compares the existing LS daily cache to KIS `manifest.trade_date` after that session's close. Until a current five-date daily window and each symbol's actual daily bar are confirmed, KRX daily-dependent lanes remain PENDING and the 25% event window is not presented as a completed scan. Actual historical dates remain visible rather than being replaced by `now`. Bare YYYY-MM-DD values are displayed as daily dates, not converted to 09:00 KST. Daily-bar dates never become synthetic 15:30 signal events. The original KIS/LS collector functions and Dockerfile remain byte-identical. New separate EOD repair sidecar reuses existing LS broker/cache API with throttling and only authentic exchange daily bars.


## EOD KRX history recovery V1.1 (2026-11-15)

**Why**: LS cached 500 symbols at 2026-11-10 while KIS READY KRX scan `20261113_143514_KRX` existed; native lazy history kept following NXT's last session, eventually `IDLE_NO_QUEUE`. This is a data repair, not a display date rewrite.

- `operations/history_repair.py` starts a separately rate-limited **KRX-only** maintenance thread in the same existing service, after a 40-second delay. No new Render service, key/token, OAuth client, or modification of native collector functions. It uses the existing `get_lazy_history_engine('KRX')` and `bootstrap_one(symbol)` for authentic LS daily OHLCV, writing to the existing LS `bars` cache through the original store. **It makes additional LS chart requests**; broker quota should be monitored. No synthetic candles are persisted.
- Requires a complete KIS KRX authority universe and verified immutable KIS READY `round_id` for the prior completed trading session. Missing or conflicting metadata means `PENDING`. Accepts 6-character alphanumeric KRX codes such as `0120G0`; these are no longer silently discarded.
- Runs only on weekends or weekdays outside 08:00–21:00 KST, pauses while original scanner is running. Default batch 24 names, 3.0-second minimum wait between LS chart calls; per-symbol cooldown for errors and 15-minute global backoff on repeated broker failures. Restores 500 existing cached names first, then expands toward the full KIS universe. The source cache must be on the existing `/data` persistent disk.
- Optional settings: `KRX_DAILY_REPAIR_ENABLED=true` (default), `KRX_DAILY_REPAIR_BATCH=24`, `KRX_DAILY_REPAIR_PACING_SECONDS=3.0`. To stop the extra LS requests: `KRX_DAILY_REPAIR_ENABLED=false`.
- `/api/operations/status` and dashboard expose `history_repair` (expected date, ready, stale, failures, reason), while `history_latest_date` remains the actual LS date. `11/13 READY` is never asserted until underlying LS bars exist. The 25% universe remains `PENDING_PARTIAL_HISTORY` until five dates and all required symbols are verified. NXT price-action evaluation remains independent.
- **Not verified by local test**: successful production LS backfill, actual full-universe completion, exchange timestamps of KIS REST-only quotes, actual order fills, and persistence checkpoint durability. Confirm through `/api/operations/status`, updating `history_repair.updated`, `history_latest_date`, `history_ready_symbols`, and `re25_universe_status`.

## V3 independent native-contract repair (2026-11-15)

This section supersedes the V1.1 backfill notes where they conflict.

### Diagnosed operational facts

At 2026-11-15 21:04 KST, the deployed `/api/operations/status?market=KRX`
reported `history_latest_date=2026-11-10`, `history_expected_date=UNAVAILABLE`,
`rows_scanned=2367`, and **no `history_repair` field**. GitHub `main` did not have
`operations/history_repair.py`. Thus the previous V2 repair was NOT deployed.
A prior KIS `/api/ledger/latest/rows?market=KRX` returned a READY snapshot
round starting `20261113_143514_KRX`, while the live full universe included
2403 KRX codes, including 36 alphanumeric IDs.

### V3 repair design

- `integration.py` constructs a **dedicated FastHistory500** with the **existing
  LS provider** (`get_engine().ls`), points it at the **same authoritative LS
  SQLite path** consumed by the operational decision projection, and selects
  `venue='KRX'` only. It does NOT reconfigure the native shared lazy history
  worker. No additional OAuth credential, no new Render service, no new order API.
- `min_bars=220`, `lookback_days=520` for the dedicated worker only; the 200MA
  can have enough source history when the listing is old enough. Actual history
  with fewer than 200 bars remains UNAVAILABLE, not manufactured.
- The KIS KRX READY `round_id` determines the last verified trading date. The
  KRX live universe must be complete and unambiguous before repair. Request
  timestamps never become trading dates.
- Bulk read of existing LS SQLite cache measures ready/stale counts. Each
  missing code is updated via the native LS `bootstrap_one`; the last stored
  date MUST equal KIS's verified completed day, with valid OHLC and volume.
  Future-dated or corrupt candles never qualify as verified history.
- Starts only weekends and weekday 21:00-08:00 KST (not during KRX/NXT
  sessions); pauses while native scanner runs. The default batch is 16 with
  >=4s between broker calls (configurable, minimum 3s for production),
  30-minute individual cooldown and 15-minute circuit backoff on 3
  consecutive failures. Existing broker quotas still must be observed.
- `KRX_DAILY_REPAIR_ENABLED=false` disables the additional LS queries without
  affecting original collectors. Optional values:
  `KRX_DAILY_REPAIR_BATCH=16`, `KRX_DAILY_REPAIR_PACING_SECONDS=4.0`.
- `history_repair.updated/failed/ready/stale/depth_lt_200/expected_date` are
  exposed in the KRX status and shown in the dashboard. `history_latest_date`
  always comes from stored real daily bars, not from the current clock.
  KRX history may remain PENDING while unsupported tickers lack bars.
- Full V1.7 memoization checks all 260 analyzed bars, not just the last 5.
  Old-candle corrections therefore trigger reevaluation.
- NXT post-acceleration coverage and decisions use independent NXT minute
  evidence; they are NOT marked incomplete solely due to KRX LS daily lag.
- Historical bars with invalid OHLC, negative volume or weekend timestamps
  are excluded from the operational read projection (the native source cache
  is not deleted or altered by the projection).

### Validation and limitations

- Unit/integration: `python -m unittest operations.test_engine -q`
  (60 tests; 59 passed in local non-Docker environment, one Docker-only native
  import test skipped).
- Native Dockerfile contract smoke: real `FastHistory500` and `HistoryStore`
  source classes extracted from the pre-existing Dockerfile, with a fake LS
  provider, passed **save -> SQLite reread -> restart no duplicate fetch ->
  KRX/NXT namespace separation**. No live LS broker request was made.
- Browser script: extracted JavaScript passes `node --check`.
- The build-time test in the existing Dockerfile additionally exercises the
  native import test when its original modules are installed.
- **Not claimed**: live LS t8410 request success, every KRX ticker having a
  valid 11/13 daily candle, actual production backfill completed, Render deploy
  success, or full-universe event coverage. These must be checked after upload.

### Post-upload production checklist

1. GitHub `main/operations` contains `history_repair.py` and all five revised
   files. Do not replace `Dockerfile`, `.github/workflows`, or `render.yaml`.
2. Confirm Render deploy is `Live` (not a failed build). The service uses
   `Dockerfile.alpha500-onebox.txt`; it has a copied `operations` directory.
3. In `/api/operations/status?market=KRX` confirm `rows_scanned` and
   `history_expected_date=2026-11-13` on Nov 15. KIS manifest must verify;
   stale or missing manifests produce PENDING, never a made-up date.
4. Check `history_repair` exists. Wait for its off-session scan and inspect
   `updated`, `failed`, `ready`, `stale`, `reason`, and `last_error`. If LS
   refuses the chart route, inspect the production broker logs rather than
   changing the displayed date.
5. Confirm `history_latest_date` advances only as real LS daily bars are saved;
   the 25% event lane remains PENDING until the five-date symbol histories
   and full scan evidence meet the integrity contract.
6. If rate limits or service degradation occur, set
   `KRX_DAILY_REPAIR_ENABLED=false` in Render and redeploy. This is a
   reversible safety switch that does not affect the original collectors.
