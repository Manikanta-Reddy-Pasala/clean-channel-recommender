# Clean-Channel Recommender (POC)

Config-driven clean-channel selection across 2G/3G/4G/5G bands. No AI —
deterministic **filter → rank → constraint-solve**. Sub-second over ~1M
candidate channels. Every scenario/rule lives in `config.yaml`; the engine has
zero per-scenario code.

## Idea

```
1M rows ── polars filter + score (from config) ──► top ~400 ──► OR-Tools CP-SAT
           (~30 ms, NOT the solver)                            picks best SET (~90 ms)
```

- **Best single channel** → filter + sort. No solver.
- **A SET of non-interfering channels** (guard band + power budget + transceiver
  count) → real combinatorics → [OR-Tools CP-SAT](https://developers.google.com/optimization/cp/cp_solver).

Prune millions first, solve on hundreds. That is why it stays under 1s.

Full code walkthrough, file by file: **[WALKTHROUGH.md](WALKTHROUGH.md)**.

## Benchmarks (8-core)

| Rows | Scenario | Total |
|------|----------|-------|
| 1M | best single | 34 ms |
| 1M | multi-channel set | 218 ms |
| 5M | multi-channel set | 447 ms |

## Run

```bash
make install                 # venv + deps
make run                     # all scenarios, 1M rows
make bench                   # 5M stress
make api                     # REST API + web UI on :8000
```

**Web UI:** run `make api`, open <http://localhost:8000/> — pick a scenario, tweak
overrides, see the recommended channels and timings. (Single static file, no build.)

CLI: `python engine.py [--rows N] [--scenario NAME] [--data file.parquet] [--json] [--list-scenarios] [--gen file.parquet]`

REST:
```bash
curl localhost:8000/scenarios
curl -X POST localhost:8000/recommend -H 'Content-Type: application/json' \
  -d '{"scenario":"urban_multi_capture","max_channels":3}'
```
Overrides: `k`, `max_channels`, `power_budget`, `min_separation_khz`. Docs at `/docs`.

## Add a scenario (no code)

```yaml
urban_multi_capture:
  allowed_techs: [3G, 4G, 5G]
  allowed_bands: [B1, B3, B40, n78, n1]
  hard_filters: { max_commercial_rssi: -50, min_capture_prob: 0.20 }
  weights: { cleanliness: 0.50, capture_prob: 0.40, power_cost: -0.10 }
  select: { mode: set, max_channels: 4, min_separation_khz: 15000, power_budget: 220, prune_to: 400 }
```

## Files

`engine.py` (filter+score+CP-SAT+CLI) · `api.py` (FastAPI + UI) · `static/index.html` (web UI) · `config.yaml` (all logic) · `Makefile` · `requirements.txt` · [`WALKTHROUGH.md`](WALKTHROUGH.md) (how it works)

## Note

Candidate data is **synthetic** here (`generate_candidates`). Production loads a
pre-scanned `.parquet`/`.csv` via `--data` / `CHANNEL_DATA`, held in memory and
refreshed on scan. Feature model (`cleanliness`, `capture_prob`, `power_required`)
is placeholder — wire real RSSI / capture-probability / regulatory band tables.
