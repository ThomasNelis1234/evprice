# EV charging price alert

Once a day, around 13:00 Brussels time, this job emails a short Dutch (BE) recommendation with a chart:
when to charge an EV over the next two days, based on Belgian quarter-hourly day-ahead
electricity prices. The day after tomorrow is shown as a forecast, clearly marked and never
used for the recommendation.

Design, assumptions and verified API details: [ARCHITECTURE.md](ARCHITECTURE.md).

## How it works

- **Published prices** (today + tomorrow, and the last 30 days for the long-run average)
  come from [energy-charts.info](https://api.energy-charts.info) (Bundesnetzagentur | SMARD.de, CC BY 4.0).
- **Forecast** for the day after tomorrow comes from the public
  [EpexPredictor](https://github.com/b3nn0/EpexPredictor) instance. No availability guarantee —
  if it is down the email still goes out and says so.
- The **cheapest consecutive window** of the configured duration is computed on published prices only.
- A GitHub Actions cron runs every 30 minutes within a window; each run is idempotent, so a
  late auction, a late runner or a crash simply means the next tick tries again. At most one
  email per day (`state/runs.json`, committed back by the workflow).

## Setup

1. Push this repository to GitHub (public is simplest; private also works within the free minutes).
2. In the Gmail account used for sending: enable 2-step verification, then create an **App Password**
   (Google Account → Security → App passwords).
3. Repository → Settings → Secrets and variables → Actions → add:
   - `GMAIL_USER` — the Gmail address
   - `GMAIL_APP_PASSWORD` — the 16-character app password
   - `RECIPIENTS` — comma-separated list of addresses to send to (kept out of the repository on purpose)
4. Edit `config.yaml` (charging duration, times, tariff).
5. Actions → "Daily EV charging price alert" → *Run workflow* with **dry_run** ticked. Download the
   `dry-run-output` artifact and check the PNG. Then run again with **force** ticked to receive the first
   real email immediately.

From then on it runs by itself.

## Local use

```bash
pip install -e .
python -m unittest discover -s tests -t .          # tests, incl. DST and failure cases
python scripts/demo.py                             # offline demo on synthetic data -> out/
python -m evprice.main --dry-run                   # real APIs, no email -> out/
GMAIL_USER=... GMAIL_APP_PASSWORD=... RECIPIENTS=a@x.be,b@y.be python -m evprice.main --force   # send now
```

`--now 2026-10-25T14:30` pretends it is another time (useful around DST changes).

## Configuration (`config.yaml`)

| Key | Meaning |
|---|---|
| `bidding_zone` | `BE`; any zone both sources support |
| `timezone` | IANA zone for all local times |
| `horizon_start_local` | Graph starts here on the run day (default 14:00) |
| `earliest_send_local` | Nothing happens before this local time (default 13:00) |
| `publication_cutoff_local` | After this, send even if tomorrow is unpublished (default 18:00) |
| `charging_duration_hours` | 2 / 4 / 6 / 8 — consecutive hours to find |
| `history_days` | Days in the long-run average |
| `recipients` | Local fallback only — in Actions the `RECIPIENTS` secret is used (one email, all in To) |
| `creg` | Quarterly CREG reimbursement tariff (c€/kWh all-in), region, validity period; `check_online` compares it with CREG's CSV |
| `cost_stack` | Your supplier's per-kWh costs on top of the day-ahead price (from the tariff sheet) — used to compute the break-even price against the CREG tariff. Set `distribution_operator` to your Fluvius zone |

Secrets and personal data (Gmail credentials, recipient addresses) live only in environment variables / GitHub Secrets.

## Guarantees

- Published and forecast prices are different Python types; the recommendation code cannot accept a forecast.
- Missing quarter-hours are reported, never interpolated.
- DST days (92 or 100 slots) are handled by computing in UTC; nothing assumes 96 slots.
- Attribution for both data sources is printed on every graph.
