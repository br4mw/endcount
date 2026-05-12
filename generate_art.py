"""
Pre-generate art (reference photo + Claude SVG + sprite PNG) for every species
that appears in the cached IUCN list files.

Run:
  python3 generate_art.py           # skip already-generated sprites
  python3 generate_art.py --force   # regenerate all sprites in new style
"""

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import vectorize as vec

CACHE_DIR = "cache"
PNG_DIR   = os.path.join(CACHE_DIR, "sprites_png")
FORCE     = "--force" in sys.argv

# ── Collect all species from cached list files ────────────────────────────────

def load_all_species():
    species = {}  # assessment_id → {scientific_name, common_name, taxonomy}

    # Common names store
    names_path = os.path.join(CACHE_DIR, "_names.json")
    names = {}
    if os.path.exists(names_path):
        with open(names_path) as f:
            names = json.load(f)

    # Taxonomy from full cached assessments
    taxonomy_map = {}
    for fname in os.listdir(CACHE_DIR):
        if not fname.startswith("assessment_"):
            continue
        with open(os.path.join(CACHE_DIR, fname)) as f:
            a = json.load(f)
        taxon = a.get("taxon", {})
        aid   = a.get("assessment_id")
        if aid:
            parts = [
                taxon.get("class_name",  "").capitalize(),
                taxon.get("order_name",  "").capitalize(),
                taxon.get("family_name", "").capitalize(),
            ]
            taxonomy_map[aid] = " / ".join(p for p in parts if p) or "animal"

    # Walk all list cache files
    for fname in sorted(os.listdir(CACHE_DIR)):
        if not (fname.startswith("list_") and fname.endswith(".json")):
            continue
        with open(os.path.join(CACHE_DIR, fname)) as f:
            data = json.load(f)
        for item in data.get("assessments", []):
            aid  = item["assessment_id"]
            sci  = item["taxon_scientific_name"]
            common   = names.get(str(aid))
            taxonomy = taxonomy_map.get(aid, "animal")
            species[aid] = {
                "scientific_name": sci,
                "common_name":     common,
                "taxonomy":        taxonomy,
            }

    return species


# ── Generation worker ─────────────────────────────────────────────────────────

def generate_one(aid, info):
    if not FORCE and os.path.exists(os.path.join(PNG_DIR, f"{aid}.png")):
        return aid, "skip"
    try:
        result = vec.prepare_species_art(
            info["scientific_name"], aid,
            common_name=info["common_name"],
            taxonomy=info["taxonomy"],
            force=FORCE,
        )
        if result["has_sprite"]:
            return aid, "ok"
        return aid, "no_sprite"
    except Exception as e:
        return aid, f"error: {e}"


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    all_species = load_all_species()
    todo = {
        aid: info for aid, info in all_species.items()
        if FORCE or not os.path.exists(os.path.join(PNG_DIR, f"{aid}.png"))
    }

    total   = len(all_species)
    already = total - len(todo)
    print(f"Total species: {total}")
    print(f"Already done:  {already}")
    print(f"To generate:   {len(todo)}")

    if not todo:
        print("Nothing to do.")
        return

    # Estimate time
    est_min = len(todo) * 10 / 60 / 5  # 10s each, 5 workers
    print(f"Estimated time with 5 workers: ~{est_min:.0f} minutes\n")

    counts = {"ok": 0, "skip": 0, "no_sprite": 0, "error": 0}
    start  = time.time()
    done   = 0

    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = {ex.submit(generate_one, aid, info): aid for aid, info in todo.items()}
        for future in as_completed(futures):
            aid, status = future.result()
            done += 1

            key = "error" if status.startswith("error") else status
            counts[key] = counts.get(key, 0) + 1

            elapsed = time.time() - start
            rate    = done / elapsed if elapsed else 0
            eta     = (len(todo) - done) / rate if rate else 0

            sym = "✓" if status == "ok" else ("·" if status == "skip" else "✗")
            print(
                f"[{done:>4}/{len(todo)}] {sym}  {aid:<12} {status:<12} "
                f"ETA {eta/60:.1f}min",
                flush=True,
            )

    elapsed = time.time() - start
    print(f"\nDone in {elapsed/60:.1f} minutes.")
    print(f"Generated: {counts['ok']}  |  Skipped: {counts['skip']}  |  "
          f"No sprite: {counts.get('no_sprite',0)}  |  Errors: {counts.get('error',0)}")


if __name__ == "__main__":
    main()
