"""Dump every college game (schedule, ESPN/DK open + close, final, the walk-forward model's
margin) to one JSON file for offline slicing — the Actions runner can reach ESPN, the Claude
sandbox can't. Nov 15 2026, Rob: "the best damn fire you can find".

  python -m scripts.cfb_dump --seasons 2024,2025,2026 --cache .cfb_cache --out cfb_dump.json
"""
from __future__ import annotations

import argparse
import json
import logging
import os

from scripts.cfb_model_open import walk
from scripts.cfb_spots import fetch_all_odds, fetch_games


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", default="2023,2024,2025,2026")
    ap.add_argument("--cache", default=".cfb_cache")
    ap.add_argument("--out", default="cfb_dump.json")
    a = ap.parse_args()
    os.makedirs(a.cache, exist_ok=True)
    seasons = [int(s) for s in a.seasons.split(",")]
    games = []
    for y in seasons:
        games += fetch_games(y, a.cache)
    odds = fetch_all_odds(games, a.cache)
    model = {r["id"]: r["proj"] for r in walk(games, odds, seasons[1] if len(seasons) > 1 else seasons[0], 40.0)}
    for g in games:
        g["odds"] = odds.get(g["id"])
        g["model_margin"] = model.get(g["id"])
    json.dump(games, open(a.out, "w"))
    print("dumped", len(games), "games,", sum(1 for g in games if g.get("model_margin") is not None), "with a model number")


if __name__ == "__main__":
    main()
