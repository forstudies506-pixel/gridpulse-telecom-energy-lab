# GridPulse: synthetic telecom energy prototype

GridPulse is a local, dependency-free prototype for exploring AI-assisted cell
tower energy optimisation. It generates deterministic synthetic city and tower
telemetry, learns a causal demand forecast, detects upward traffic surprises,
and simulates a safety-constrained controller that changes active capacity and
power modes.

This is a **simulation and research demonstrator**. It does not connect to
radio equipment, infer geographic coverage radius, or make a real-world energy
savings claim.

## Run the browser experience

From this directory:

```bash
python3 dashboard.py
```

Then open <http://127.0.0.1:8501>.

The first viewport is a full-bleed **Intelligence For Industries** landing page
with the supplied CloudFront video, retro display type, animated platform stats,
and responsive mobile navigation. Its scrollable case study reports a
deterministic simulation outcome, peak demand windows, and city-local
availability risk zones. **Get Started** and **Product** open the GridPulse Lab
at `/lab.html`, where the synthetic coverage, fault, response, and tower
controls use the same Python simulation API. The old telemetry dashboard is no
longer the main UI; the API remains available for experiments and tests at
`POST /api/simulation`.

Do not double-click `index.html` or serve this folder with a static-only server:
the page needs the `POST /api/simulation` endpoint supplied by `dashboard.py`.
The browser now shows a clear startup error for those cases, but the supported
path is still the command above.

The retained simulation API accepts the random seed, simulated days, number of
cities/towers, scheduled-event probability, unexpected-anomaly probability,
and synthetic fault probability. It includes:

- actual versus forecast demand;
- a future-only holdout error card;
- always-on, fixed-schedule, and adaptive energy comparisons;
- city roll-ups and a per-tower interval replay;
- `sleep`, `eco`, `normal`, and `boost` controller modes;
- an explicit **Demonstrate user surge** scenario;
- a synthetic 5 × 5 city-local coverage grid with primary/backup cells;
- telemetry-fault validation and a deterministic technical-response queue;
- a local full-run playback with an animated demand/forecast profile, live
  users/coverage readouts, chronological fault/event/remedy records, and
  bounded DOM virtualization for longer runs;
- a simulation-only help-team deployment control that is enabled at a current
  coverage issue and never contacts real people or systems;
- recent event/spike alerts and a client-generated aggregate CSV export.

The replay advances synthetic **60-minute samples** by default. It is not a
real-time control feed.

The dashboard's **fault chance** control injects missing, invalid, or
inconsistent synthetic readings. A detected fault puts the controller into
fail-safe boost, marks affected coverage cells, and creates simulation-only
technical-response incidents with deterministic team IDs and estimated arrival
times. No real team, person, vehicle, emergency service, network device, or
external system is contacted.

## Run the terminal demo

```bash
python3 telecom_model.py --days 2 --seed 7
python3 telecom_model.py --days 3 --seed 4 --surge-demo --json
```

## Run the checks

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile telecom_model.py dashboard.py tests/test_telecom_model.py tests/test_dashboard.py
```

The browser server can also be smoke-tested without leaving a background
process running:

```bash
python3 - <<'PY'
import json
from dashboard import run_simulation

result = run_simulation({"days": 2, "cities": 2, "towers_per_city": 2, "seed": 7})
print(json.dumps(result["summary"], indent=2, sort_keys=True))
PY
```

For a browser-facing HTTP check, confirm that these all return successfully:

```bash
curl -I http://127.0.0.1:8501/
curl -I http://127.0.0.1:8501/dashboard.js
curl http://127.0.0.1:8501/api/health
```

## Publish to GitHub

Recommended repository name: `gridpulse-telecom-energy-lab`

Suggested GitHub description:

> GridPulse is a dependency-free synthetic telecom energy lab that forecasts tower demand, detects telemetry faults, simulates adaptive capacity control, and replays coverage risk without connecting to live network equipment.

From this project directory, create an empty repository on GitHub and run:

```bash
git init
git add .
git commit -m "feat: add GridPulse industry intelligence lab"
git branch -M main
git remote add origin https://github.com/<your-user>/gridpulse-telecom-energy-lab.git
git push -u origin main
```

Do not commit API keys, passwords, `.env` files, local logs, `.vercel/`, or
private runtime data. The included `.gitignore` excludes those local artifacts.

### GitHub repository topics

`telecom`, `energy-optimization`, `synthetic-data`, `time-series-forecasting`,
`fault-detection`, `simulation`, `vercel`, `python`, `javascript`

## Deploy to Vercel

The repository includes a dependency-free Vercel adapter:

- `api/simulation.py` exposes `POST /api/simulation` as a Vercel Python Function.
- `api/health.py` exposes `GET /api/health`.
- `vercel.json` configures the file-based Python functions.
- `index.html`, `lab.html`, CSS, JavaScript, fonts, and assets remain static files.

After pushing the repository to GitHub, import it in Vercel, select the
repository, and deploy with no build command and no output directory. Vercel
will serve the static landing/lab pages and run the Python API functions. The
runtime uses only Python's standard library, so no dependency installation is
required.

If using the Vercel CLI locally, install Node.js and the CLI first, then run:

```bash
npm install --global vercel
vercel login
vercel
vercel --prod
```

The current local environment does not include Node.js/Vercel CLI. GitHub
upload and Vercel deployment therefore require either the Vercel web import
flow or a machine where the CLI has been installed and authenticated.

## Model and controller notes

### Causal forecast

`train_forecaster()` uses a small ridge-guarded ordinary-least-squares model
implemented in `telecom_model.py`; no scikit-learn installation is required.
For a tower at time `t`, its demand/user lags and rolling values are computed
from observations strictly earlier than `t`. Current time, event, weather,
static city/tower metadata, and installed capacity are allowed as input
features. Current demand, current users, `telemetry_anomaly`, and
`surge_demo` are not model features.

The time axis is split chronologically. Calibration residuals are estimated
from the training history only. Each output row includes `evaluation_split`,
`forecast_origin`, `forecast_horizon_minutes`, and calibration metadata.

### Sudden-demand detection

The detector is intentionally explainable. It combines a positive forecast
residual with positive causal rate growth. Downward residuals are not reported
as rising-demand spikes, and future rows cannot change an earlier row's score.
The dashboard also reports detector precision/recall against the synthetic
anomaly labels; those labels are only for evaluation and are not inputs to the
forecast.

### Controller

The controller keeps immutable installed capacity and a nonzero conceptual
coverage floor. `boost` restores the full installed capacity; `sleep` only
sleeps optional capacity while retaining the floor. Upgrades are immediate on
events/spikes, while downgrades use a three-interval dwell and hysteresis.
Missing or invalid demand/capacity/power telemetry enters fail-safe `boost`.
If demand exceeds installed capacity, the model reports overload and unmet
demand instead of hiding it.

The power ledger is an explicit assumption: a fixed idle/coverage component
plus a capacity-dependent component. Always-on, adaptive, and a fixed
night-schedule benchmark are all reported with their service levels. The
numbers are useful for comparing controller behaviour inside this synthetic
world; they are not a forecast of an operator's electricity bill.

### Coverage and response limitations

Coverage is a **synthetic cell-availability proxy**. Cells are assigned to the
nearest synthetic tower in normalized city-local coordinates and checked using
simulated active capacity plus optional backup capacity. It does not estimate
RF propagation, signal strength, geographic radius, subscriber reach, or an
SLA. An uncovered cell is only a result of the simulation's rules for one
interval.

Telemetry faults are synthetic validation cases. Demand spikes, traffic
anomalies, overloads, and scheduled events are not proof of equipment failure.
The response queue is a deterministic local simulation, not a live dispatch or
emergency-response integration.

## Performance notes

The default workload is 14 days × 3 cities × 3 towers at 60-minute intervals.
On the development machine, repeated warm-process measurements after the
coverage/operations feature were approximately:

- model pipeline: **725 ms median**;
- dashboard response assembly: **910 ms median**;
- response JSON size: approximately **3.1 MB** with the default settings.

The dashboard reuses one row-record conversion for summary, coverage, replay,
and dispatch projections, caps returned dispatch records at 12, and uses a
fixed 5 × 5 grid rather than a heavyweight map library. These measurements are
local synthetic benchmarks, not a production latency guarantee.

## Main files

- `telecom_model.py` — data generator, causal forecaster, detector, controller,
  benchmark, CLI.
- `dashboard.py` — local standard-library HTTP server and JSON adapter.
- `index.html`, `styles.css`, `main.js` — landing page and navigation.
- `lab.html`, `lab.css`, `lab.js` — GridPulse Lab controls and simulation views.
- `dashboard.js` — retained legacy dashboard frontend for reference.
- `tests/test_telecom_model.py` — causal/model/controller behaviour tests.
- `tests/test_dashboard.py` — dashboard response/replay contract tests.
