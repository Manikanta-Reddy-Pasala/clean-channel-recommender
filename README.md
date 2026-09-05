# Clean-Channel Recommender (POC)

Config-driven recommendation engine for clean-channel selection across
2G / 3G / 4G / 5G bands. No AI — deterministic filter + rank + constraint solve.

**Goal:** sub-second recommendation over ~1M candidate channels, with every
scenario/rule living in `config.yaml` (zero code changes per scenario).

## Why this design

Two different problems hide in "recommend a channel":

1. **Best single channel** → filter + score + sort. Pure ranking. No solver.
2. **A SET of N non-interfering channels** (guard bands, power budget, limited
   transceivers) → real combinatorics → [OR-Tools CP-SAT](https://developers.google.com/optimization/cp/cp_solver).

The mistake that makes naive versions slow (~4s) is feeding all million rows
into loops or into the solver. **Prune first, solve last:**

```
1M rows ── polars filter + score (from config) ──► top ~400 ──► CP-SAT picks best SET
           (~30 ms, NOT the solver)                            (~90 ms)
```

CP-SAT earns its place only on the small hard combinatorial core.

## Benchmarks (8-core box)

| Rows | Scenario | After filter | Filter+score | CP-SAT | Total |
|------|----------|--------------|--------------|--------|-------|
| 1M | best single (rank only) | 276k | 29 ms | — | **29 ms** |
| 1M | multi-channel set | 497k | 41 ms | 95 ms | **135 ms** |
| 1M | rural set | 296k | 24 ms | 71 ms | **94 ms** |
| 5M | multi-channel set | 2.5M | 240 ms | 99 ms | **339 ms** |

All well under the 1s target.

## Run

Setup once:
```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
# or: make install
```

### 1. CLI

```bash
python engine.py                              # all scenarios, 1M synthetic rows
python engine.py --rows 5000000               # stress
python engine.py --scenario urban_multi_capture
python engine.py --list-scenarios             # names + modes
python engine.py --scenario urban_best_single --json   # machine-readable
python engine.py --gen candidates.parquet     # write synthetic data file
python engine.py --data candidates.parquet --scenario rural_wide_capture
```

| Flag | Purpose |
|------|---------|
| `--rows N` | synthetic candidate count (default 1M) |
| `--scenario NAME` | run one scenario (default: all) |
| `--data PATH` | load pre-scanned `.parquet`/`.csv` instead of synthetic |
| `--gen PATH` | write synthetic candidates to a file and exit |
| `--json` | emit JSON instead of tables |
| `--list-scenarios` | print scenario names and exit |
| `--seed N` | rng seed |

### 2. Makefile shortcuts

```bash
make install    # venv + deps
make run        # all scenarios, 1M rows
make bench      # 5M-row stress
make json       # JSON output
make list       # list scenarios
make gen        # write candidates.parquet
make api        # serve REST API on :8000
```

### 3. REST API

Data is loaded once at startup and held in memory; each request is just the
sub-second filter+solve path.

```bash
uvicorn api:app --host 0.0.0.0 --port 8000        # or: make api
# point at real data + size via env:
CHANNEL_DATA=candidates.parquet uvicorn api:app --port 8000
```

Endpoints:
```bash
curl localhost:8000/health
curl localhost:8000/scenarios

# recommend a non-interfering set
curl -X POST localhost:8000/recommend -H 'Content-Type: application/json' \
  -d '{"scenario":"urban_multi_capture","max_channels":3}'

# best single, override k
curl -X POST localhost:8000/recommend -H 'Content-Type: application/json' \
  -d '{"scenario":"urban_best_single","k":3}'
```

Per-request overrides (optional): `k`, `max_channels`, `power_budget`,
`min_separation_khz` — config stays the default.
Interactive docs at `http://localhost:8000/docs`.

## Config-driven — add a scenario without touching code

```yaml
urban_multi_capture:
  allowed_techs: [3G, 4G, 5G]
  allowed_bands: [B1, B3, B40, n78, n1]
  hard_filters: { max_commercial_rssi: -50, min_capture_prob: 0.20 }
  weights: { cleanliness: 0.50, capture_prob: 0.40, power_cost: -0.10 }
  select:
    mode: set                 # or top_k
    max_channels: 4           # transceiver limit
    min_separation_khz: 15000 # guard band
    power_budget: 220
    prune_to: 400             # candidates fed to solver
```

Engine reads: whitelist → filter, weights → score, select → constraints.

## Files

| File | Purpose |
|------|---------|
| `engine.py` | filter+score (polars) → CP-SAT set selection → benchmark + CLI |
| `api.py` | FastAPI REST endpoint (data held in memory) |
| `config.yaml` | all scenario/band/rule logic |
| `Makefile` | run/bench/api/gen shortcuts |
| `requirements.txt` | deps |

## Notes

- Candidate data here is **synthetic** (`generate_candidates`). Production loads
  a pre-scanned table (Mongo/Parquet), kept in memory and refreshed on scan —
  the generation time is not part of a request.
- Feature model (`cleanliness`, `capture_prob`, `power_required`) is placeholder;
  wire real RSSI / capture-probability / regulatory band tables for production.
