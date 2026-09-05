#!/usr/bin/env python3
"""
REST API for the channel recommender.

Candidate data is loaded ONCE at startup and held in memory, so each request is
just filter + score + solve (the sub-second path). In production point
CHANNEL_DATA at a pre-scanned .parquet and refresh it on a scan schedule.

Env:
  CHANNEL_CONFIG   config file           (default config.yaml)
  CHANNEL_DATA     .parquet/.csv table   (default: synthetic)
  CHANNEL_ROWS     synthetic row count   (default 1000000)

Run:
  uvicorn api:app --host 0.0.0.0 --port 8000
Then:
  GET  /scenarios
  POST /recommend            {"scenario": "urban_multi_capture"}
  POST /recommend            {"scenario": "urban_best_single", "k": 3}
  POST /recommend            full override, see ScenarioOverride below
"""
import os
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import engine

CFG = engine.load_config(os.environ.get("CHANNEL_CONFIG", "config.yaml"))
_data_path = os.environ.get("CHANNEL_DATA")
if _data_path:
    DF = engine.load_candidates(_data_path)
    _source = _data_path
else:
    DF = engine.generate_candidates(CFG, int(os.environ.get("CHANNEL_ROWS", 1_000_000)))
    _source = f"synthetic:{DF.height}"

app = FastAPI(title="Clean-Channel Recommender", version="0.1")


class Req(BaseModel):
    scenario: str
    # optional per-request overrides (config stays the default)
    k: Optional[int] = None
    max_channels: Optional[int] = None
    power_budget: Optional[int] = None
    min_separation_khz: Optional[int] = None


@app.get("/health")
def health():
    return {"status": "ok", "candidates": DF.height, "source": _source}


@app.get("/scenarios")
def scenarios():
    return {
        name: {"mode": s["select"]["mode"], "allowed_techs": s["allowed_techs"]}
        for name, s in CFG["scenarios"].items()
    }


@app.post("/recommend")
def recommend(req: Req):
    if req.scenario not in CFG["scenarios"]:
        raise HTTPException(404, f"unknown scenario: {req.scenario}")

    # shallow copy so overrides don't mutate the loaded config
    import copy
    cfg = copy.deepcopy(CFG)
    sel = cfg["scenarios"][req.scenario]["select"]
    if req.k is not None:
        sel["k"] = req.k
    if req.max_channels is not None:
        sel["max_channels"] = req.max_channels
    if req.power_budget is not None:
        sel["power_budget"] = req.power_budget
    if req.min_separation_khz is not None:
        sel["min_separation_khz"] = req.min_separation_khz

    r = engine.recommend(DF, cfg, req.scenario)
    return engine.result_to_records(r)
