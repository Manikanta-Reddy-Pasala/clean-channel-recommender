# Code Walkthrough

How the recommender actually works, file by file. Read `config.yaml` first,
then `engine.py` top to bottom — that is the whole system. `api.py` and the web
UI are thin wrappers around one function.

Four files, ~470 lines total:

| File | Lines | Job |
|------|-------|-----|
| `config.yaml` | 96 | Every rule. Bands, filters, weights, constraints. |
| `engine.py` | 282 | Data, filter, score, solve, CLI. |
| `api.py` | 94 | Load once, serve `/recommend`. |
| `static/index.html` | 184 | One page, no build step. |

---

## 1. The problem, and why half of it needs a solver

You have a scanned list of candidate radio channels — roughly a million rows,
one per `tech x band x physical channel`. Each row carries measured features:
how busy it is, how likely a handset attaches, how much power it costs to use.

Two different questions get asked of that list:

**"Give me the best channel."** Rank by a score, take the top one. A sort. No
solver — every row is judged on its own.

**"Give me the best four channels I can run at once."** Now the rows are not
independent. Two channels too close in frequency interfere, so picking one
forbids the other. The total power draw must stay under a budget. You have a
fixed number of transceivers. The best set is not the top four individually —
the second-best channel might sit 5 MHz from the best one and be unusable
alongside it. That is a constrained combinatorial choice, and it is what
[OR-Tools CP-SAT](https://developers.google.com/optimization/cp/cp_solver) is for.

The whole design follows from one number: a solver over a million rows is
hopeless, a solver over a few hundred is instant. So the pipeline is

```
1M rows ──► vectorized filter + score ──► top ~400 ──► CP-SAT ──► answer
            (polars, ~40 ms)                          (~100 ms)
```

Prune cheaply, solve expensively — but only on what survives. Total stays under
a second at 1M rows, and 350 ms at 5M.

---

## 2. `config.yaml` — the only place rules live

Two sections.

**`techs`** is reference data: each technology's channel bandwidth and the
frequency range of each band.

```yaml
5G:
  channel_bw_khz: 20000
  bands:
    n78: { f_low_khz: 3300000, f_high_khz: 3800000 }
```

**`scenarios`** is the interesting half. Each scenario is a complete
specification of one kind of request:

```yaml
urban_multi_capture:
  allowed_techs: [3G, 4G, 5G]                 # hard whitelist
  allowed_bands: [B1, B3, B40, n78, n1]
  hard_filters:                               # per-row reject thresholds
    max_commercial_rssi: -50
    min_capture_prob: 0.20
  weights:                                    # linear score
    cleanliness: 0.50
    capture_prob: 0.40
    power_cost: -0.10                         # negative = penalty
  select:
    mode: set                                 # set -> CP-SAT; top_k -> sort only
    max_channels: 4                           # transceivers available
    min_separation_khz: 15000                 # guard band
    power_budget: 220
    prune_to: 400                             # rows handed to the solver
```

The engine has no `if scenario == ...` anywhere. It reads these keys and builds
the filter, the objective, and the constraints from them. Adding a scenario is
editing this file — no code, no deploy.

---

## 3. `engine.py`

### `generate_candidates` (L45)

Synthetic data, and **only** because this is a POC. It flattens `techs x bands`
into a pick list, draws a random tech/band per row, places a center frequency
uniformly inside that band, then draws the measured features:

| Column | Distribution | Meaning |
|--------|-------------|---------|
| `commercial_rssi` | normal(−70, 15) dBm | how strong the incumbent operator is — higher is busier |
| `noise_floor` | normal(−105, 5) dBm | background noise |
| `interference` | uniform 0..1 | congestion; `cleanliness = 1 − interference` |
| `capture_prob` | beta(2, 3) | how likely a handset camps on us |
| `power_required` | int 20..99 | tx power cost |

Seeded (`seed=7`), so every run produces identical numbers.

**In production you delete none of this — you just stop calling it.** Pass
`--data scan.parquet` and `load_candidates` (L32) reads your real scan instead.
The rest of the pipeline never knows the difference; it only needs those column
names to exist.

### `filter_and_score` (L94) — the cheap million-row pass

Builds a polars boolean mask out of config, one condition at a time:

```python
conds = [pl.col("tech").is_in(scn["allowed_techs"]),
         pl.col("band").is_in(scn["allowed_bands"])]
if "max_commercial_rssi" in hard:
    conds.append(pl.col("commercial_rssi") <= hard["max_commercial_rssi"])
```

Then the score, straight from the weights:

```python
score = (pl.col("cleanliness")            * w.get("cleanliness", 0.0)
       + pl.col("capture_prob")           * w.get("capture_prob", 0.0)
       + (pl.col("power_required")/100.0) * w.get("power_cost", 0.0))
```

Two things to notice. `power_required` is divided by 100 so a 20–99 integer
lands on the same 0–1 scale as the other features — otherwise its weight would
swamp them regardless of the number you wrote in the config. And every weight
uses `.get(..., 0.0)`, so a scenario that omits a feature simply scores it zero
rather than crashing.

This is one vectorized pass. No Python loop touches a row. ~40 ms for 1M rows,
and it is where roughly a third of total runtime goes.

### `select_set` (L134) — the solver

Only reached when `mode: set`. First it throws away everything but the best
`prune_to` rows (default 400). **This is the load-bearing line of the whole
project.** CP-SAT gets hundreds of rows, never a million.

One boolean per candidate — "do we pick this channel?":

```python
x = [m.NewBoolVar(f"x{i}") for i in range(n)]
m.Add(sum(x) <= sel["max_channels"])                                   # transceivers
m.Add(sum(int(power[i]) * x[i] for i in range(n)) <= sel["power_budget"])
```

Interference is pairwise: two channels closer than the guard band cannot both
be chosen. Written naively that is 400² = 160,000 constraints. Instead the
candidates are sorted by frequency, so the inner loop can stop the moment it
clears the guard band:

```python
order = np.argsort(freq); fs = freq[order]; bws = bw[order]
break_at = sep + int(bw.max())          # widest requirement any pair can have
for a in range(n):
    i = order[a]
    for b in range(a + 1, n):
        gap = fs[b] - fs[a]
        if gap >= break_at:
            break                       # sorted: nothing further conflicts either
        if gap * 2 < sep * 2 + int(bws[a]) + int(bws[b]):
            m.Add(x[i] + x[order[b]] <= 1)
```

Only genuinely conflicting pairs become constraints — typically a small
fraction of the worst case.

`min_separation_khz` is the gap required between channel **edges**, not
centers, so two channels conflict when their centers are closer than
`sep + (bw_i + bw_j)/2`. That per-pair requirement is why the scan breaks at
`sep + max_bw` — the widest any pair can ask for — and then tests each pair
exactly. (Written doubled, `gap*2 < sep*2 + bw_i + bw_j`, to stay in integers.)

Then the solver's answer is only read if it actually solved:

```python
status = solver.Solve(m)
name = solver.StatusName(status)
if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
    return cand.head(0), name
```

The empty selection is always feasible, so `INFEASIBLE` cannot occur — but the
one-second cap can return `UNKNOWN` on a hard instance, and reading variable
values from that is undefined. The status travels out through `recommend` as
`solver_status`, and shows up in the CLI, the JSON, and the API response.

The objective maximizes total score. CP-SAT is an integer solver, so float
scores are scaled by 1,000,000 and cast to `int64`; six decimal places is far
more resolution than the ranking needs. The solver is capped at one second with
eight workers, and the picked rows come back sorted by score.

### `recommend` (L176) — the dispatcher

Thirty lines. Calls `filter_and_score`, then branches once on `mode`: `top_k`
takes the head of the ranking and reports `solve_ms = 0`; `set` calls the
solver. Times each stage with `time.perf_counter` and returns the timings
alongside the result — that is where the millisecond numbers in the output and
the web UI come from.

### `main` (L215)

Argument parsing and printing. `--list-scenarios` and `--gen` both exit early.
`--gen` writes the synthetic table and stops — parquet or CSV, chosen by the
file extension. `--json` swaps the pretty tables for machine-readable output.
With no `--scenario`, every scenario in the config runs in turn.

---

## 4. `api.py`

The important part is at import time, not in a request handler:

```python
DF = engine.load_candidates(_data_path) if _data_path else engine.generate_candidates(...)
```

Candidates load **once** at startup and stay in memory. A request is then only
filter + score + solve, which is the sub-second path. If data were loaded per
request, every call would pay the read.

`POST /recommend` deep-copies the config before applying per-request overrides
(`k`, `max_channels`, `power_budget`, `min_separation_khz`), so one caller's
tweak cannot leak into the next request's defaults. Unknown scenario names
return 404. `GET /health` reports the row count and where the data came from.

Configured by environment: `CHANNEL_CONFIG`, `CHANNEL_DATA`, `CHANNEL_ROWS`.

Because `DF` is module-level, every uvicorn worker holds its own copy of the
table. Four workers on 1M rows means four times the memory.

## 5. `static/index.html`

One file, no framework, no build. On load it fetches `/scenarios` to populate
the dropdown; the Recommend button POSTs to `/recommend` with any non-empty
override fields and renders the returned rows into a table, with the timings
above it. It is served by FastAPI itself at `/`.

---

## 6. Where the time actually goes

At 1M rows, `urban_multi_capture`:

| Stage | Time |
|-------|------|
| filter + score (1M rows) | ~45 ms |
| CP-SAT (400 rows) | ~173 ms |
| **total** | **~218 ms** |

At 5M rows the same scenario totals ~447 ms. Both measured on an 8-core box.

Synthetic data generation (~1 s at 1M rows) is a one-time startup cost and is
excluded — production loads a file instead.

Raising `prune_to` is the fastest way to make this slow: the constraint count
grows quadratically in the pruned set. Lowering it risks pruning away a channel
that would have fit the set well. 400 is a POC guess, not a tuned value.

---

## 7. Extending it

**A new scenario** — add a block under `scenarios:` in `config.yaml`. No code.

**A new feature to score on** — add the column to your data, then add one term
to the `score` expression in `filter_and_score` and a weight in the config.

**A new hard filter** — add three lines next to the existing `if "..." in hard`
checks.

**A new constraint** (say, at most one channel per band) — one `m.Add(...)` in
`select_set`, reading its parameter from `sel`.

---

## 8. Sharp edges

Honest list of what this POC does not do:

- **The full filtered set is sorted** — ~500k rows — when only the top few
  hundred are needed. `top_k` would be cheaper than `sort`.
- **`noise_floor` and `bw_khz` are carried but never used.** They exist for the
  real feature model to pick up.
- **Data loads eagerly.** `pl.read_parquet` pulls the whole table into memory.
  `pl.scan_parquet` plus a streaming collect would handle files larger than RAM.
- **The feature model is invented.** `cleanliness`, `capture_prob` and
  `power_required` are plausible-looking random draws, not measurements, and
  nothing here has been validated against a real scan.

Two earlier entries are now fixed rather than known: the guard band accounts
for channel bandwidth, and the solver's status is checked before its answer is
trusted.
