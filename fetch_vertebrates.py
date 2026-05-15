"""
Fetches full IUCN assessments for all species in EX/EW/CR/EN/VU categories,
then writes a vertebrate index (Mammalia + Aves + Reptilia) to cache/_vertebrates.json.

Strategy:
  1. Page through every list page for each target category (cached).
  2. Fetch the full assessment for any ID not already cached (10 parallel workers).
  3. Scan all cached assessments; collect vertebrates by class_name.
  4. Write cache/_vertebrates.json: [{assessment_id, scientific_name, common_name,
     class_name, order_name, family_name, category_code}]

Run:
  python3 fetch_vertebrates.py           # skip already-cached assessments
  python3 fetch_vertebrates.py --force   # re-fetch everything
"""

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

IUCN_TOKEN = "wttvRUqcni78ic1SfdVCFzKxcgSXHsbBG1gr"
IUCN_BASE  = "https://api.iucnredlist.org/api/v4"
IUCN_HDRS  = {
    "Authorization": f"Bearer {IUCN_TOKEN}",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0",
}

CACHE_DIR   = os.path.join(os.path.dirname(__file__), "cache")
NAMES_PATH  = os.path.join(CACHE_DIR, "_names.json")
VERT_PATH   = os.path.join(CACHE_DIR, "_vertebrates.json")
CATEGORIES  = ["EX", "EW", "CR", "EN", "VU"]
FORCE       = "--force" in sys.argv

# IUCN API returns class_name in uppercase
TARGET_CLASSES = {
    "MAMMALIA",
    "AVES",
    "REPTILIA", "TESTUDINES", "SQUAMATA", "CROCODYLIA", "RHYNCHOCEPHALIA",
    "AMPHIBIA",
    "ACTINOPTERYGII",
    "CHONDRICHTHYES",
}

os.makedirs(CACHE_DIR, exist_ok=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

def cache_path(key):
    safe = key.replace("/", "_").replace(" ", "_")
    return os.path.join(CACHE_DIR, f"{safe}.json")


def load_cache(key):
    p = cache_path(key)
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return None


def save_cache(key, data):
    with open(cache_path(key), "w") as f:
        json.dump(data, f)


def load_names():
    if os.path.exists(NAMES_PATH):
        with open(NAMES_PATH) as f:
            return json.load(f)
    return {}


# ── Step 1: collect all assessment IDs from list pages ───────────────────────

def collect_all_ids():
    ids = {}  # assessment_id → category_code
    for cat in CATEGORIES:
        page = 1
        while True:
            key  = f"list_{cat}_p{page}"
            data = load_cache(key)
            if data is None:
                try:
                    r = requests.get(
                        f"{IUCN_BASE}/red_list_categories/{cat}",
                        headers=IUCN_HDRS,
                        params={"latest": True, "page": page},
                        timeout=15,
                    )
                    r.raise_for_status()
                    data = r.json()
                    save_cache(key, data)
                    time.sleep(0.05)
                except Exception as e:
                    print(f"  [warn] {cat} p{page}: {e}")
                    break
            items = data.get("assessments", [])
            if not items:
                break
            for item in items:
                ids[item["assessment_id"]] = item.get("red_list_category_code", cat)
            print(f"  {cat} p{page}: {len(items)} species  (total so far: {len(ids)})", flush=True)
            if len(items) < 100:
                break
            page += 1
    return ids


# ── Step 2: fetch full assessments ───────────────────────────────────────────

def fetch_assessment(assessment_id):
    """Fetch one assessment. Sleeps 0.5s after each attempt to stay ~2 req/s per worker."""
    key = f"assessment_{assessment_id}"
    if not FORCE and os.path.exists(cache_path(key)):
        return assessment_id, "cached"
    for attempt in range(3):
        try:
            r = requests.get(
                f"{IUCN_BASE}/assessment/{assessment_id}",
                headers=IUCN_HDRS,
                timeout=20,
            )
            time.sleep(0.5)   # polite pause — keeps each worker at ~1 req/s
            if r.status_code == 429:
                time.sleep(30)  # back off on rate limit
                continue
            r.raise_for_status()
            data = r.json()
            save_cache(key, data)
            return assessment_id, "fetched"
        except Exception as e:
            time.sleep(2)
            if attempt == 2:
                return assessment_id, f"error: {e}"
    return assessment_id, "error: max retries"


def fetch_all_assessments(all_ids):
    to_fetch = [
        aid for aid in all_ids
        if FORCE or not os.path.exists(cache_path(f"assessment_{aid}"))
    ]
    already = len(all_ids) - len(to_fetch)
    print(f"\nAssessments: {len(all_ids)} total | {already} cached | {len(to_fetch)} to fetch")
    if not to_fetch:
        return

    # 2 workers × ~1 req/s each = ~2 req/s sustained, no burst spikes
    est = len(to_fetch) / 2 / 60
    print(f"Estimated fetch time (2 workers, ~2 req/s): ~{est:.0f} min\n")

    done = errors = 0
    start = time.time()

    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = {ex.submit(fetch_assessment, aid): aid for aid in to_fetch}
        for future in as_completed(futures):
            aid, status = future.result()
            done += 1
            if status.startswith("error"):
                errors += 1

            if done % 200 == 0 or done == len(to_fetch):
                elapsed = time.time() - start
                rate    = done / elapsed if elapsed else 0
                eta     = (len(to_fetch) - done) / rate / 60 if rate else 0
                print(
                    f"  [{done:>6}/{len(to_fetch)}]  errors: {errors}  "
                    f"ETA {eta:.1f} min",
                    flush=True,
                )


# ── Step 3: build vertebrate index ───────────────────────────────────────────

def build_vertebrate_index(all_ids):
    names = load_names()
    vertebrates = []

    for fname in os.listdir(CACHE_DIR):
        if not fname.startswith("assessment_"):
            continue
        try:
            with open(os.path.join(CACHE_DIR, fname)) as f:
                a = json.load(f)
        except Exception:
            continue

        taxon = a.get("taxon", {})
        cls   = taxon.get("class_name", "") or ""
        if cls not in TARGET_CLASSES:
            continue

        aid  = a.get("assessment_id")
        if aid not in all_ids:
            continue  # outside our target categories

        # Common name: names store first, then assessment's own common_names
        common = names.get(str(aid))
        if not common:
            for n in taxon.get("common_names", []):
                if n.get("language") == "eng":
                    common = n["name"]
                    break

        vertebrates.append({
            "assessment_id":   aid,
            "scientific_name": taxon.get("scientific_name", ""),
            "common_name":     common,
            "class_name":      cls,
            "order_name":      taxon.get("order_name", ""),
            "family_name":     taxon.get("family_name", ""),
            "category_code":   all_ids.get(aid, ""),
        })

    vertebrates.sort(key=lambda x: (x["class_name"], x["order_name"], x["scientific_name"]))

    with open(VERT_PATH, "w") as f:
        json.dump(vertebrates, f, indent=2)

    return vertebrates


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=== Step 1: Collecting all assessment IDs from list pages ===")
    all_ids = collect_all_ids()
    print(f"\nTotal species found: {len(all_ids)}")

    print("\n=== Step 2: Fetching full assessments ===")
    fetch_all_assessments(all_ids)

    print("\n=== Step 3: Building vertebrate index ===")
    verts = build_vertebrate_index(all_ids)

    by_class = {}
    for v in verts:
        by_class.setdefault(v["class_name"], []).append(v)

    print(f"\nVertebrates indexed: {len(verts)}")
    for cls in sorted(by_class):
        print(f"  {cls:<20} {len(by_class[cls])}")

    print(f"\nWritten to: {VERT_PATH}")


if __name__ == "__main__":
    main()
