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

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

python engine.py                          # all scenarios, 1M synthetic rows
python engine.py --rows 5000000           # stress
python engine.py --scenario urban_multi_capture
```

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
| `engine.py` | filter+score (polars) → CP-SAT set selection → benchmark |
| `config.yaml` | all scenario/band/rule logic |
| `requirements.txt` | deps |

## Notes

- Candidate data here is **synthetic** (`generate_candidates`). Production loads
  a pre-scanned table (Mongo/Parquet), kept in memory and refreshed on scan —
  the generation time is not part of a request.
- Feature model (`cleanliness`, `capture_prob`, `power_required`) is placeholder;
  wire real RSSI / capture-probability / regulatory band tables for production.
