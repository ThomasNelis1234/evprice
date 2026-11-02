# EV Charging Price Alert — Architecture & Assumptions (MVP)

Status: approved 2026-10-20; Phase 0 (API probe) completed, findings in §2
Date: 2026-10-20
Scope: v1 — one user, Belgian bidding zone, Europe/Brussels, email only, one daily run

---

## 1. Decisions taken

| Topic | Decision | Source |
|---|---|---|
| Price basis | Raw wholesale day-ahead prices, EUR/MWh, no taxes/markup | user |
| Availability window (car at home) | Not in v1 | user |
| Forecast (day+2) | Shown on the graph as context only; never used for stats or recommendations | user |
| Averages and cheapest window | Computed on published days only (today from 14:00 + tomorrow) | user |
| Horizon start | Fixed 14:00 local on the run day, regardless of actual run time | user |
| Email sender | Gmail account, via SMTP with an App Password | user |
| Hosting / scheduler | GitHub Actions | user |
| Primary actual-price source | api.energy-charts.info (free, JSON, no key, 15-min BE data) | proposal |
| Secondary actual-price source | ENTSO-E Transparency API (token required) — fallback, added in hardening phase | proposal |
| Forecast source | epexpredictor.batzill.com public API (`region=BE`) | requirements |
| 30-day history | Re-fetched from energy-charts on every run; no database in v1 | proposal |
| Run state | Small JSON file committed back to the repo by the workflow | proposal |

## 2. Assumptions — verified in Phase 0 (probed 2026-10-20 21:05 CEST)

A1 ✅ **energy-charts.** `GET https://api.energy-charts.info/price?bzn=BE&start=<ISO>&end=<ISO>` returns `{license_info, unix_seconds[], price[], unit, deprecated}`, `unit` = `"EUR / MWh"`, 900-second spacing, no nulls. Two findings that shape the implementation:
- A date-only `end` (e.g. `end=2026-10-22`) returned only 2026-10-20 — the last day is dropped. With explicit offset-aware timestamps (`start=2026-09-20T00:00+02:00&end=2026-10-21T23:45+02:00`) the response ran through 2026-10-21 23:45 local, i.e. tomorrow was present at 21:05. **Always pass full ISO timestamps with offset; `end` is then inclusive.**
- One call for 32 days returned 3,072 rows (32 × 96), so history + today + tomorrow come from a single request.
- Source attribution for BE is "Bundesnetzagentur | SMARD.de, CC BY 4.0" — must be credited in the graph footer.

A2 ✅ **EpexPredictor.** `GET https://epexpredictor.batzill.com/prices` with `region=BE`, `unit=EUR_PER_MWH`, `timezone=Europe/Brussels`, `hours=<n>` returns `{prices: [{startsAt, total}], knownUntil}`. Findings:
- `unit` defaults to **CT_PER_KWH** — it must be set explicitly to `EUR_PER_MWH`; enum: `CT_PER_KWH | EUR_PER_KWH | EUR_PER_MWH`.
- `timezone` defaults to Europe/Berlin (same offset as Brussels, but set it explicitly).
- The series also covers today and tomorrow; there is **no per-item actual/forecast flag**. `knownUntil` (observed: `2026-10-21T23:45:00+02:00`) marks the last published slot. The ingest layer therefore keeps only entries with `startsAt > knownUntil` and within day+2, and discards the rest — the published prices always come from energy-charts, never from the predictor.
- `knownUntil` doubles as an independent "is tomorrow published yet" signal for the retry loop.
- Other params noted for later: `surcharge`, `taxPercent` (contract pricing), `evaluation=true` (back-test mode), `hourly`.

A3 ✅ Timestamps are Unix seconds (energy-charts) and ISO 8601 with offset (predictor) — unambiguous across DST.

A4–A6 unchanged (Gmail App Password, GitHub Actions minutes, single recipient).

Fixtures: the sandbox used for development cannot reach either host from a shell, so Phase 1 fixtures are synthesized to the verified schemas above; the first live run in GitHub Actions saves both raw responses to `tests/fixtures/live/` for regression use.

## 3. Daily timeline

```
14:00 local ─────────────── 23:45 │ 00:00 ───────────── 23:45 │ 00:00 ───────────── 23:45
   today (ACTUAL, 40 slots)       │   tomorrow (ACTUAL, 96*)   │  day+2 (FORECAST, 96*)
   ◄──────── published: stats + recommendation ────────►       ◄── context only ──►
```
`*` 92 or 100 on DST-transition days — never hard-coded.

## 4. Data model

Actual and forecast prices are different types, not a flag on one type. This makes it impossible to accidentally feed a forecast into the stats or the window calculation.

```
PriceSeries (abstract)
  index:      tz-aware DatetimeIndex, Europe/Brussels, 15-min, strictly increasing, no gaps
  values:     float, EUR/MWh
  source:     str            # "energy-charts", "entsoe", "epexpredictor"
  fetched_at: datetime (UTC)

ActualPrices(PriceSeries)      # only accepted by stats/window functions
ForecastPrices(PriceSeries)    # only accepted by the plotting function

DailyBundle
  run_date:        date
  actual:          ActualPrices        # today 14:00 → tomorrow 23:45 (may be shorter if tomorrow missing)
  forecast:        ForecastPrices | None
  history_30d:     ActualPrices        # previous 30 full days, for the long-run average
  tomorrow_published: bool
  warnings:        list[str]           # "forecast unavailable", "tomorrow not yet published", "stale"

Recommendation
  start, end:      datetime (local)
  avg_price:       float  (EUR/MWh)
  duration_hours:  int
  basis:           Literal["actual"]   # forecast is never a basis in v1
```

Validation on ingest (hard failures, never patched with invented values):
- count of slots for each calendar day matches the expected DST-aware count;
- no NaN, no duplicate timestamps;
- unit field matches expectation;
- data for today after 14:00 is present (otherwise nothing can be sent).

## 5. Processing flow

```
GitHub Actions cron (every 30 min, 11:00–17:00 UTC)
  │
  ├─ load state/runs.json
  ├─ already sent for today?  → exit 0 (idempotent)
  ├─ local time < 13:00?       → exit 0
  │
  ├─ fetch actual (energy-charts): today 14:00 → tomorrow 23:45, plus last 30 days
  ├─ validate → ActualPrices
  ├─ tomorrow missing AND local time < cutoff (18:00)? → exit 0, retry next cron tick
  │
  ├─ fetch forecast (epexpredictor) → ForecastPrices | None (+warning)
  │
  ├─ compute: period average (actual), 30-day average (history), cheapest window(s) (actual)
  ├─ render PNG (matplotlib, Agg backend)
  ├─ send email (Gmail SMTP): short text + inline PNG + attachment
  │
  ├─ write state/runs.json  {date, status, sent_at, warnings, sources, counts}
  └─ commit state file back to repo
```

Why a 30-minute cron instead of one 13:00 run: GitHub cron fires in UTC and may start late; a repeated idempotent run gives us the publication-delay retry and the DST offset handling for free. Each early-exit run costs a few seconds.

## 6. Cheapest-window algorithm

- Input: `ActualPrices` only, duration `D` hours (configurable: 2/4/6/8), `N = 4·D` slots.
- Sliding window of length N over the contiguous actual series; score = mean price.
- Output: best window, plus the best non-overlapping second window if it is within 10 % of the best (so the user sees an alternative).
- Windows are not allowed to cross into day+2 (forecast) in v1 — guaranteed by the type system, since the function does not accept `ForecastPrices`.
- Unit test cases: normal day, DST short day (92 slots), DST long day (100 slots), tomorrow missing (window limited to today's remaining slots, warning emitted), duration longer than available data (no recommendation, warning).

## 7. Graph (PNG, ~1600×800)

- x-axis: local time, day boundaries marked, date labels per day.
- Solid line: actual prices (today + tomorrow).
- Dashed line, distinct colour, in a shaded region: day+2 forecast. Optional light band of ±2 ct/kWh (≈ ±20 EUR/MWh, the author's reported 2-day MAE for BE) around the forecast line.
- Horizontal line: average of the displayed actual period.
- Horizontal line: 30-day average.
- Highlighted span(s): recommended charging window(s).
- Legend states "Forecast = estimate, not market price".
- Footer: generated-at timestamp, timezone, unit (EUR/MWh), data sources and fetched-at times, any warnings.

## 8. Email

- Transport: `smtplib` + STARTTLS to `smtp.gmail.com:587`.
- Auth: `GMAIL_USER` + `GMAIL_APP_PASSWORD` from environment (GitHub Secrets).
- Content: multipart — plain-text summary, responsive HTML with the chart inline (CID), PNG also attached.
- Language: Dutch (Belgium) throughout — email, chart, warnings. Formatting lives in `nl.py`
  (lowercase day/month names, `11u45` clock times, decimal comma); no system locale is needed.
- Subject: `⚡ EV laden: morgen 11u45–15u45 · gem. 89 EUR/MWh (4 u, vr 23 okt)` — key facts first so
  phones show them uncut; `⚠️ … — gegevens onvolledig` when warnings exist.
- HTML: 600px-max table layout with inline styles, Outlook fixed-width fallback in `<!--[if mso]>`
  comments, two stat columns that stack on narrow screens (inline-block + max-width, no media query
  needed; a media query additionally tightens padding on phones). Icons are emoji — no remote images.
  Cards: best window (with % saving vs. the period average), alternative, two averages, forecast
  (clearly marked as an estimate), warnings, chart, source footer.
- Dry run also writes `ev-prices-<date>.html` (chart path swapped in for the CID) for browser preview.

## 8b. Comparison with the CREG reimbursement tariff (`creg.py`)

- The CREG tariff is the quarterly all-in maximum (energy + network + levies + 6 % btw, c€/kWh) an
  employer may reimburse for home charging a company car. FOD Financiën derives it from the CREG
  monthly series with a two-month lag (Q1 = Aug–Oct, Q2 = Nov–Jan, Q3 = Feb–Apr, Q4 = May–Jul), per
  region of residence. The CREG profile is a household with an EV: digital meter, 8 000 kWh/year,
  7,36 kW average monthly peak. Reference: creg.be → "Tarief terugbetaling thuisladen bedrijfswagens".
- Our prices are raw day-ahead EUR/MWh, so the comparison models the household's *marginal* cost
  per kWh on a dynamic contract, from the supplier tariff sheet in `config.yaml` (`cost_stack`):
  `all_in(P) = (a + b·P)·(1+btw) + GSC/WKK + green option + Fluvius kWh-tarief + federale accijns +
  energiefonds + optional capacity surcharge`. Inverting gives the **break-even day-ahead price**,
  drawn as a purple line on the chart (it replaces the period-average line; that average stays in the
  stat tiles). Fixed yearly amounts are excluded — they do not depend on *when* you charge. The
  capaciteitstarief is included as the share charging causes: `tariff (€/kW/j) × extra_peak_kw ÷
  kwh_per_year` (config `cost_stack.capacity`; extra_peak_kw 0 = treat as sunk). The email states the
  resulting c€/kWh and the configured charging-power cap.
- Email: hero pill shows all-in c€/kWh and the margin vs CREG (green under, red over); a CREG card
  lists cost and margin for the best window, the alternative and the period average; the subject
  carries `· N c onder/boven CREG`.
- Maintenance: the quarterly value sits in `config.yaml` with its validity period. The run warns in
  the email when that period has passed. With `check_online: true` the runner also downloads CREG's
  monthly CSV, averages the three months behind the current quarter and warns when it differs from
  the configured value by more than 0,05 c€/kWh. The CSV layout could not be verified when this was
  written: the reader is tolerant (`;`/`,`, decimal comma, wide or long layout, €/kWh or c€/kWh) and
  every failure degrades to "not checked" — it never blocks the email. The raw CSV is saved to
  `tests/fixtures/live/creg_tariff_ev_<date>.csv` on live runs so the parser can be confirmed.

## 9. Configuration

Non-secret (`config.yaml`, committed):
```yaml
bidding_zone: BE
timezone: Europe/Brussels
horizon_start_local: "14:00"
earliest_send_local: "13:00"
publication_cutoff_local: "18:00"
charging_duration_hours: 4
history_days: 30
recipient: thomasnelis1234@gmail.com
sources:
  actual: https://api.energy-charts.info
  forecast: https://epexpredictor.batzill.com
```
Secret (environment / GitHub Secrets only): `GMAIL_USER`, `GMAIL_APP_PASSWORD`, later `ENTSOE_TOKEN`.

## 10. Failure handling

| Failure | Behaviour |
|---|---|
| energy-charts timeout / 5xx | 3 retries with backoff; then (hardening phase) ENTSO-E fallback; else exit non-zero, no email, state records failure |
| Tomorrow not yet published | Exit quietly, retry at next tick until cutoff; after cutoff send with only today's remaining slots + warning |
| Forecast API down / malformed | Send anyway; shaded region shows "forecast unavailable" |
| Slot count wrong / NaN | Hard failure for that source; never interpolate |
| SMTP failure | 3 retries; state not marked sent, so next tick retries the whole run |
| Duplicate trigger | `state/runs.json` says sent for today → exit 0 |
| Workflow crash mid-run | state unchanged → next tick re-runs; email is the last step before state write, so at most one duplicate in the narrow window between send and commit (accepted for v1; a pre-send "sending" marker closes it in hardening) |

Logging: structured log lines to the Actions log; `state/runs.json` keeps the last 60 run records as a lightweight audit trail.

## 11. Repository layout

```
.
├── ARCHITECTURE.md
├── README.md
├── config.yaml
├── pyproject.toml
├── src/evprice/
│   ├── config.py        # load yaml + env
│   ├── http.py          # requests + retry
│   ├── timeutil.py      # DST-safe slot arithmetic
│   ├── sources/
│   │   ├── energy_charts.py
│   │   ├── epex_predictor.py
│   │   └── entsoe.py    # phase 6
│   ├── models.py        # ActualPrices, ForecastPrices, DailyBundle, Recommendation
│   ├── validate.py
│   ├── windows.py       # cheapest-window algorithm
│   ├── plot.py
│   ├── mailer.py
│   ├── state.py
│   └── main.py          # orchestrates one run
├── scripts/demo.py      # offline end-to-end run on synthetic data
├── tests/
│   ├── synth.py         # synthetic payloads matching the verified schemas
│   ├── fixtures/live/   # raw responses saved by each real run
│   └── test_*.py
├── state/runs.json
└── .github/workflows/daily.yml
```

Dependencies: `requests`, `pandas`, `matplotlib`, `pyyaml` only (tests use the standard library `unittest`; retries and the clock are injected rather than pulled from `tenacity`/`freezegun`). No database, no web framework.

## 12. Implementation phases

| Phase | Deliverable | Test of done |
|---|---|---|
| 0 — Probe ✅ | Two throwaway scripts that call both APIs for BE and print shape, timestamps, unit, slot counts; recorded responses saved as fixtures | Assumptions A1–A3 confirmed or corrected in writing |
| 1 — Ingest ✅ | `sources/`, `models.py`, `validate.py` | Unit tests pass on normal day, DST days, missing-tomorrow, malformed response |
| 2 — Compute ✅ | `windows.py`, averages | Unit tests incl. edge cases in §6 |
| 3 — Graph ✅ | `plot.py` producing the PNG from a `DailyBundle` | Visual check of a real day + a forecast-unavailable day |
| 4 — Email ✅ (code; first real send pending secrets) | `mailer.py`, one manual send | Email received with inline image and attachment |
| 5 — Automate ✅ (code; first live run pending repo) | `main.py`, `state.py`, workflow, state commit-back | Two consecutive triggers on the same day send exactly one email |
| 6 — Harden | ENTSO-E fallback, pre-send marker, retry tuning, README | Fault-injection tests (source down, SMTP down) |

## 13. What you need to do (outside the code)

1. Create a GitHub repository and tell me its name.
2. On the Gmail account: enable 2-step verification, then create an App Password (Google Account → Security → App passwords). Store it directly as the repository secret `GMAIL_APP_PASSWORD` together with `GMAIL_USER` — do not paste it into chat.
3. Optional but recommended now because approval takes days: register at transparency.entsoe.eu and email transparency@entsoe.eu for REST API access, for the Phase 6 fallback and for future self-hosting of the forecast model.

## 14. Known limits of v1

- Wholesale prices only: the real cost per kWh under a Belgian dynamic contract adds a supplier margin, grid tariff, levies and VAT. Window ranking is unaffected if these are flat; the capacity tariff and time-dependent grid fees are not modelled.
- Forecast accuracy is roughly ±2 ct/kWh on average and worse during scarcity peaks; it is informational.
- Single recipient, single zone, single duration. Multi-user requires the database phase from the original plan.
- The predictor's public endpoint has no availability guarantee; self-hosting is the eventual answer.
