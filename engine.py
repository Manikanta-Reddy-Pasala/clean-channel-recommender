#!/usr/bin/env python3
"""
Config-driven clean-channel recommendation POC.

Proves: <1s end-to-end on ~1M candidate channels, with ALL scenario logic in
config.yaml (zero code branches per scenario).

Pipeline
  1. Vectorized filter + score over the full candidate set (polars). ~ms.
  2. If the scenario asks for a SET of mutually-compatible channels, hand the
     pruned top-N to CP-SAT (interference / power / count constraints). <1s.
  3. Otherwise just return top-K from the ranking. No solver.

Run:
  python engine.py                 # runs all scenarios on 1M synthetic rows
  python engine.py --rows 5000000  # stress it
  python engine.py --scenario urban_multi_capture
"""
import argparse
import json
import time
import yaml
import numpy as np
import polars as pl
from ortools.sat.python import cp_model


def load_config(path: str = "config.yaml") -> dict:
    return yaml.safe_load(open(path))


def load_candidates(path: str) -> pl.DataFrame:
    """Load a pre-scanned candidate table (.parquet or .csv) for production use."""
    if path.endswith(".parquet"):
        return pl.read_parquet(path)
    if path.endswith(".csv"):
        return pl.read_csv(path)
    raise ValueError(f"unsupported data file (want .parquet/.csv): {path}")


# --------------------------------------------------------------------------- #
# Synthetic candidate universe. In production this is your measured/scanned data
# (one row per tech x band x physical channel), loaded from Mongo/Parquet/scan.
# --------------------------------------------------------------------------- #
def generate_candidates(cfg: dict, n_rows: int, seed: int = 7) -> pl.DataFrame:
    rng = np.random.default_rng(seed)

    # Flatten techs x bands from config into a pick list.
    combos = []
    for tech, tspec in cfg["techs"].items():
        bw = tspec["channel_bw_khz"]
        for band, bspec in tspec["bands"].items():
            combos.append((tech, band, bw, bspec["f_low_khz"], bspec["f_high_khz"]))

    idx = rng.integers(0, len(combos), size=n_rows)
    tech = np.array([combos[i][0] for i in idx])
    band = np.array([combos[i][1] for i in idx])
    bw = np.array([combos[i][2] for i in idx], dtype=np.int64)
    f_low = np.array([combos[i][3] for i in idx], dtype=np.int64)
    f_high = np.array([combos[i][4] for i in idx], dtype=np.int64)

    # A channel center frequency placed on the band grid.
    span = f_high - f_low
    freq_khz = f_low + (rng.random(n_rows) * span).astype(np.int64)

    # Measured features (what your scanner would report).
    commercial_rssi = rng.normal(-70, 15, n_rows)          # dBm; higher = busier
    noise_floor = rng.normal(-105, 5, n_rows)              # dBm
    interference = rng.random(n_rows)                       # 0 clean .. 1 congested
    capture_prob = np.clip(rng.beta(2, 3, n_rows), 0, 1)   # attractiveness to UE
    power_required = rng.integers(20, 100, n_rows)          # arbitrary tx-power units

    cleanliness = 1.0 - interference                        # derived feature

    return pl.DataFrame(
        {
            "channel_id": np.arange(n_rows, dtype=np.int64),
            "tech": tech,
            "band": band,
            "bw_khz": bw,
            "freq_khz": freq_khz,
            "commercial_rssi": commercial_rssi,
            "noise_floor": noise_floor,
            "cleanliness": cleanliness,
            "capture_prob": capture_prob,
            "power_required": power_required,
        }
    )


# --------------------------------------------------------------------------- #
# Step 1 — build filter + score straight from the scenario config. Vectorized.
# --------------------------------------------------------------------------- #
def filter_and_score(df: pl.DataFrame, scn: dict) -> pl.DataFrame:
    hard = scn.get("hard_filters", {})
    w = scn["weights"]

    conds = [
        pl.col("tech").is_in(scn["allowed_techs"]),
        pl.col("band").is_in(scn["allowed_bands"]),
    ]
    if "max_commercial_rssi" in hard:
        conds.append(pl.col("commercial_rssi") <= hard["max_commercial_rssi"])
    if "min_capture_prob" in hard:
        conds.append(pl.col("capture_prob") >= hard["min_capture_prob"])

    mask = conds[0]
    for c in conds[1:]:
        mask = mask & c

    # Linear score from config weights. features are already 0..1-ish;
    # power normalized to keep the penalty on the same scale.
    score = (
        pl.col("cleanliness") * w.get("cleanliness", 0.0)
        + pl.col("capture_prob") * w.get("capture_prob", 0.0)
        + (pl.col("power_required") / 100.0) * w.get("power_cost", 0.0)
    )

    return (
        df.filter(mask)
        .with_columns(score.alias("score"))
        .sort("score", descending=True)
    )


# --------------------------------------------------------------------------- #
# Step 2 — CP-SAT: choose a mutually-compatible SET from the pruned candidates.
# Constraints, all sourced from config:
#   - at most max_channels picks (transceiver count)
#   - any two picks within min_separation_khz conflict -> not both
#   - sum(power_required) over picks <= power_budget
# Objective: maximize total score.
# --------------------------------------------------------------------------- #
def select_set(ranked: pl.DataFrame, sel: dict) -> pl.DataFrame:
    prune_to = sel.get("prune_to", 400)
    cand = ranked.head(prune_to)
    n = cand.height
    if n == 0:
        return cand

    freq = cand["freq_khz"].to_numpy()
    power = cand["power_required"].to_numpy()
    # scale float score to int; CP-SAT is integer.
    score_i = (cand["score"].to_numpy() * 1_000_000).astype(np.int64)

    m = cp_model.CpModel()
    x = [m.NewBoolVar(f"x{i}") for i in range(n)]

    m.Add(sum(x) <= sel["max_channels"])
    m.Add(sum(int(power[i]) * x[i] for i in range(n)) <= sel["power_budget"])

    # Interference: pairwise guard band on the pruned set only (n^2 but n<=few hundred).
    sep = sel["min_separation_khz"]
    order = np.argsort(freq)
    fs = freq[order]
    for a in range(n):
        i = order[a]
        for b in range(a + 1, n):
            if fs[b] - fs[a] >= sep:
                break  # sorted: no further j conflicts with i
            j = order[b]
            m.Add(x[i] + x[j] <= 1)

    m.Maximize(sum(int(score_i[i]) * x[i] for i in range(n)))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 1.0
    solver.parameters.num_search_workers = 8
    solver.Solve(m)

    chosen = [i for i in range(n) if solver.Value(x[i]) == 1]
    return cand[chosen].sort("score", descending=True)


# --------------------------------------------------------------------------- #
def recommend(df: pl.DataFrame, cfg: dict, scenario: str) -> dict:
    scn = cfg["scenarios"][scenario]
    sel = scn["select"]

    t0 = time.perf_counter()
    ranked = filter_and_score(df, scn)
    t1 = time.perf_counter()

    if sel["mode"] == "top_k":
        result = ranked.head(sel.get("k", 5))
        t2 = time.perf_counter()
        solve_ms = 0.0
    else:
        result = select_set(ranked, sel)
        t2 = time.perf_counter()
        solve_ms = (t2 - t1) * 1000

    return {
        "scenario": scenario,
        "mode": sel["mode"],
        "candidates_after_filter": ranked.height,
        "returned": result.height,
        "filter_score_ms": (t1 - t0) * 1000,
        "solve_ms": solve_ms,
        "total_ms": (t2 - t0) * 1000,
        "result": result.select(
            ["tech", "band", "freq_khz", "cleanliness", "capture_prob",
             "power_required", "score"]
        ),
    }


def result_to_records(r: dict) -> dict:
    """JSON-serializable view of a recommend() result."""
    out = {k: v for k, v in r.items() if k != "result"}
    out["result"] = r["result"].to_dicts()
    return out


def main():
    ap = argparse.ArgumentParser(
        description="Config-driven clean-channel recommender POC")
    ap.add_argument("--rows", type=int, default=1_000_000,
                    help="synthetic candidate rows (ignored if --data given)")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--scenario", default=None,
                    help="run one scenario (default: all)")
    ap.add_argument("--data", default=None,
                    help="load pre-scanned candidates from .parquet/.csv")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--json", action="store_true",
                    help="emit JSON instead of tables")
    ap.add_argument("--list-scenarios", action="store_true",
                    help="print scenario names and exit")
    ap.add_argument("--gen", metavar="PATH",
                    help="write synthetic candidates to a .parquet/.csv and exit")
    args = ap.parse_args()

    cfg = load_config(args.config)

    if args.list_scenarios:
        for name, scn in cfg["scenarios"].items():
            print(f"{name:24s} mode={scn['select']['mode']:6s} "
                  f"techs={scn['allowed_techs']}")
        return

    if args.data:
        df = load_candidates(args.data)
        if not args.json:
            print(f"# loaded {df.height:,} candidates from {args.data}\n")
    else:
        t = time.perf_counter()
        df = generate_candidates(cfg, args.rows, seed=args.seed)
        gen_ms = (time.perf_counter() - t) * 1000
        if args.gen:
            (df.write_parquet(args.gen) if args.gen.endswith(".parquet")
             else df.write_csv(args.gen))
            print(f"# wrote {df.height:,} candidates -> {args.gen}")
            return
        if not args.json:
            print(f"# generated {df.height:,} candidate channels in {gen_ms:.0f} ms "
                  f"(one-time; production loads pre-scanned data)\n")

    scenarios = [args.scenario] if args.scenario else list(cfg["scenarios"])

    if args.json:
        print(json.dumps([result_to_records(recommend(df, cfg, s))
                          for s in scenarios], indent=2))
        return

    for s in scenarios:
        r = recommend(df, cfg, s)
        print(f"=== scenario: {r['scenario']}  (mode={r['mode']}) ===")
        print(f"  after filter : {r['candidates_after_filter']:,} candidates")
        print(f"  filter+score : {r['filter_score_ms']:.1f} ms")
        if r["mode"] == "set":
            print(f"  CP-SAT solve : {r['solve_ms']:.1f} ms")
        print(f"  TOTAL        : {r['total_ms']:.1f} ms  "
              f"({'OK <1s' if r['total_ms'] < 1000 else 'OVER 1s'})")
        print(f"  recommended {r['returned']} channel(s):")
        with pl.Config(tbl_rows=20, tbl_hide_dataframe_shape=True):
            print(r["result"])
        print()


if __name__ == "__main__":
    main()
