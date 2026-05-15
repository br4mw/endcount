import os
import json
import re
import threading
import anthropic
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, render_template, abort, request, send_file, jsonify
from dotenv import load_dotenv

load_dotenv()

import vectorize as vec

app = Flask(__name__)

IUCN_TOKEN = "wttvRUqcni78ic1SfdVCFzKxcgSXHsbBG1gr"
IUCN_BASE = "https://api.iucnredlist.org/api/v4"
IUCN_HEADERS = {
    "Authorization": f"Bearer {IUCN_TOKEN}",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

VERT_PATH = os.path.join(CACHE_DIR, "_vertebrates.json")

def _load_vertebrates():
    by_cat = {}
    if not os.path.exists(VERT_PATH):
        return by_cat
    with open(VERT_PATH) as f:
        for e in json.load(f):
            by_cat.setdefault(e.get("category_code", ""), []).append(e)
    return by_cat

_vertebrate_by_cat = _load_vertebrates()

CATEGORY_LABELS = {
    "EX": "Extinct",
    "EW": "Extinct in the Wild",
    "CR": "Critically Endangered",
    "EN": "Endangered",
    "VU": "Vulnerable",
    "NT": "Near Threatened",
    "LC": "Least Concern",
}

# Population in the wild for species with no individuals remaining
WILD_POPULATION_ZEROS = {"EX", "EW"}

CATEGORY_ORDER = ["EX", "EW", "CR", "EN", "VU", "NT", "LC"]

# ── Background art generation ─────────────────────────────────────────────────
_art_executor   = ThreadPoolExecutor(max_workers=2)
_generating     = set()   # assessment_ids currently being generated
_generating_lock = threading.Lock()


def _art_task(scientific_name, assessment_id, common_name, taxonomy):
    try:
        vec.prepare_species_art(
            scientific_name, assessment_id,
            common_name=common_name, taxonomy=taxonomy,
        )
    finally:
        with _generating_lock:
            _generating.discard(assessment_id)


def _ensure_art(scientific_name, assessment_id, common_name, taxonomy):
    """
    Returns current art status immediately.
    If art is incomplete and not already being generated, fires a background task.
    """
    status = vec.art_status(assessment_id)
    if not (status["has_image"] and status["has_sprite"]):
        with _generating_lock:
            if assessment_id not in _generating:
                _generating.add(assessment_id)
                _art_executor.submit(
                    _art_task, scientific_name, assessment_id, common_name, taxonomy
                )
        status["generating"] = True
    else:
        status["generating"] = False
    return status


def cache_path(key):
    safe = key.replace("/", "_").replace(" ", "_")
    return os.path.join(CACHE_DIR, f"{safe}.json")


def load_cache(key):
    path = cache_path(key)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return None


def save_cache(key, data):
    with open(cache_path(key), "w") as f:
        json.dump(data, f)


def fetch_species_list(category, page=1):
    cache_key = f"list_{category}_p{page}"
    cached = load_cache(cache_key)
    if cached:
        return cached

    r = requests.get(
        f"{IUCN_BASE}/red_list_categories/{category}",
        headers=IUCN_HEADERS,
        params={"latest": True, "page": page},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    save_cache(cache_key, data)
    return data


def fetch_assessment(assessment_id):
    cache_key = f"assessment_{assessment_id}"
    cached = load_cache(cache_key)
    if cached:
        return cached

    r = requests.get(
        f"{IUCN_BASE}/assessment/{assessment_id}",
        headers=IUCN_HEADERS,
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    save_cache(cache_key, data)
    return data


def strip_html(text):
    if not text:
        return ""
    return re.sub(r"<[^>]+>", "", text).strip()


CATEGORY_POPULATION_ESTIMATES = {
    # IUCN criteria thresholds — used as fallback estimates when no figure is cited.
    # These represent the upper bound of the category definition (conservative estimate).
    "CR": 250,
    "EN": 2500,
    "VU": 10000,
    "NT": 20000,
    "LC": 100000,
}


def extract_population(assessment):
    """
    Return an estimated wild population as an integer.
    - EX / EW: 0 (none remain in the wild)
    - Otherwise: try supplementary_info, then parse from text,
      then fall back to IUCN category threshold estimates.
    Returns None only if category is completely unknown.
    """
    category_code = assessment.get("red_list_category", {}).get("code", "")

    if category_code in WILD_POPULATION_ZEROS:
        return 0

    supp = assessment.get("supplementary_info", {})
    raw = supp.get("population_size")
    if raw is not None:
        try:
            return int(raw)
        except (ValueError, TypeError):
            pass

    # Parse from documentation text — rationale and population sections
    doc = assessment.get("documentation", {})
    search_text = " ".join(filter(None, [
        strip_html(doc.get("rationale", "")),
        strip_html(doc.get("population", "")),
    ]))

    # Match patterns: "~200 individuals", "fewer than 500 mature", "2,500–3,000 adults"
    matches = re.findall(
        r"(?:fewer than|less than|approximately|~|about|around|estimated|only|as few as)?\s*"
        r"([\d,]+)\s*(?:–|-|to)?\s*(?:[\d,]+\s*)?"
        r"(?:individuals?|mature individuals?|adults?|specimens?|birds?|animals?|plants?)",
        search_text,
        re.IGNORECASE,
    )
    if matches:
        try:
            return int(matches[0].replace(",", ""))
        except ValueError:
            pass

    # Fall back to IUCN category threshold estimate
    return CATEGORY_POPULATION_ESTIMATES.get(category_code, None)


def generate_summary(assessment):
    cache_key = f"summary_{assessment['assessment_id']}"
    cached = load_cache(cache_key)
    if cached:
        return cached.get("summary", "")

    if not ANTHROPIC_API_KEY:
        return ""

    taxon = assessment.get("taxon", {})
    doc = assessment.get("documentation", {})
    category = assessment.get("red_list_category", {}).get("description", {}).get("en", "")
    trend = assessment.get("population_trend") or "unknown"

    common_names = [n["name"] for n in taxon.get("common_names", []) if n.get("language") == "eng"]
    common = common_names[0] if common_names else None

    facts = f"""
Scientific name: {taxon.get('scientific_name', '')}
Common name: {common or 'unknown'}
Status: {category}
Kingdom: {taxon.get('kingdom_name', '')}
Class: {taxon.get('class_name', '')}
Order: {taxon.get('order_name', '')}
Family: {taxon.get('family_name', '')}
Population trend: {trend}
Year published: {assessment.get('year_published', '')}
Range: {strip_html(doc.get('range', ''))[:600] or 'Unknown'}
Ecology: {strip_html(doc.get('ecology', ''))[:600] or 'Unknown'}
Threats: {strip_html(doc.get('threats', ''))[:600] or 'Unknown'}
Rationale: {strip_html(doc.get('rationale', ''))[:600] or 'Unknown'}
""".strip()

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        messages=[
            {
                "role": "user",
                "content": (
                    "Write a short, moving 2-paragraph summary about this species for a memorial art website. "
                    "Be factual but evocative — write about what this animal was like, where it lived, "
                    "and what led to its decline. Keep it under 120 words total. Do not use headers.\n\n"
                    + facts
                ),
            }
        ],
    )

    summary = message.content[0].text.strip()
    save_cache(cache_key, {"summary": summary})
    return summary


# ── Art asset routes ─────────────────────────────────────────────────────────

@app.route("/api/source/<int:assessment_id>")
def source_image(assessment_id):
    path = os.path.join(vec.IMG_DIR, f"{assessment_id}.png")
    if not os.path.exists(path):
        abort(404)
    resp = send_file(path, mimetype="image/png")
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@app.route("/api/sprite-svg/<int:assessment_id>")
def sprite_svg(assessment_id):
    path = os.path.join(vec.SVG_DIR, f"{assessment_id}.svg")
    if not os.path.exists(path):
        abort(404)
    resp = send_file(path, mimetype="image/svg+xml")
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@app.route("/api/sprite-png/<int:assessment_id>")
def sprite_png(assessment_id):
    path = os.path.join(vec.PNG_DIR, f"{assessment_id}.png")
    if not os.path.exists(path):
        abort(404)
    resp = send_file(path, mimetype="image/png")
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@app.route("/api/art-status/<int:assessment_id>")
def api_art_status(assessment_id):
    status = vec.art_status(assessment_id)
    with _generating_lock:
        status["generating"] = assessment_id in _generating
    return jsonify(status)


@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    return jsonify(_search(q, limit=25))


# ── Search index ──────────────────────────────────────────────────────────────
_search_index = None

def _build_search_index():
    """Build once from all cached list files + names store + assessment caches."""
    global _search_index
    if _search_index is not None:
        return

    store = _load_names_store()

    # Taxonomy from cached assessments
    tax_map   = {}   # aid → "Class / Order / Family"
    class_map = {}   # aid → class_name
    for fname in os.listdir(CACHE_DIR):
        if not fname.startswith("assessment_"):
            continue
        try:
            with open(os.path.join(CACHE_DIR, fname)) as f:
                a = json.load(f)
            t   = a.get("taxon", {})
            aid = a.get("assessment_id")
            if not aid:
                continue
            parts = [t.get("class_name","").capitalize(),
                     t.get("order_name","").capitalize(),
                     t.get("family_name","").capitalize()]
            tax_map[aid]   = " ".join(p for p in parts if p).lower()
            class_map[aid] = t.get("class_name","").lower()
        except Exception:
            pass

    entries = {}   # aid → dict
    for fname in sorted(os.listdir(CACHE_DIR)):
        if not (fname.startswith("list_") and fname.endswith(".json")):
            continue
        try:
            with open(os.path.join(CACHE_DIR, fname)) as f:
                data = json.load(f)
        except Exception:
            continue
        for item in data.get("assessments", []):
            aid  = item["assessment_id"]
            sci  = item.get("taxon_scientific_name", "")
            code = (item.get("red_list_category_code")
                    or item.get("code")
                    or (item.get("red_list_category") or {}).get("code", ""))
            common = store.get(str(aid))
            entries[aid] = {
                "assessment_id":   aid,
                "scientific_name": sci,
                "common_name":     common,
                "category_code":   code,
                "category_label":  CATEGORY_LABELS.get(code, code),
                "_search_blob":    " ".join(filter(None, [
                    (common or "").lower(),
                    sci.lower(),
                    tax_map.get(aid, ""),
                    CATEGORY_LABELS.get(code, "").lower(),
                ])),
            }

    _search_index = list(entries.values())


def _search(q, limit=25):
    _build_search_index()

    tokens = q.lower().split()
    if not tokens:
        return []

    scored = []
    for entry in _search_index:
        blob   = entry["_search_blob"]
        common = (entry["common_name"] or "").lower()
        sci    = entry["scientific_name"].lower()

        # All tokens must appear somewhere in the blob
        if not all(tok in blob for tok in tokens):
            continue

        # Relevance score
        score = 0
        qt = q.lower()
        if common == qt:                        score += 100
        elif common.startswith(qt):             score += 80
        elif qt in common:                      score += 60
        if sci == qt:                           score += 50
        elif sci.startswith(qt):               score += 35
        elif qt in sci:                         score += 20
        # Boost species with art ready
        if os.path.exists(os.path.join(vec.PNG_DIR, f"{entry['assessment_id']}.png")):
            score += 5

        scored.append((score, entry))

    scored.sort(key=lambda x: (-x[0], x[1]["common_name"] or x[1]["scientific_name"]))
    return [e for _, e in scored[:limit]]


@app.route("/api/particle-data/<int:assessment_id>")
def particle_data(assessment_id):
    path = os.path.join(CACHE_DIR, f"particle_data_{assessment_id}.json")
    if not os.path.exists(path):
        abort(404)
    resp = send_file(path, mimetype="application/json")
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@app.route("/api/stamps/<int:assessment_id>")
def stamp_data(assessment_id):
    """
    Pre-compute stamp positions server-side from the source image.
    Returns JSON: { stamps: [{x,y,r,g,b,bright}, ...], width, height }
    """
    cache_key = f"stamps_v2_{assessment_id}"
    cached = load_cache(cache_key)
    if cached:
        return jsonify(cached)

    source_path = os.path.join(vec.IMG_DIR, f"{assessment_id}.png")
    if not os.path.exists(source_path):
        abort(404)

    from PIL import Image
    img = Image.open(source_path).convert("RGB")
    w, h = img.size
    pixels = img.load()

    grid = 5
    candidates = []
    for y in range(0, h, grid):
        for x in range(0, w, grid):
            r, g, b = pixels[x, y]
            bright = (r + g + b) / 3
            if bright < 220:
                candidates.append({
                    "x": round(x / w, 4),
                    "y": round(y / h, 4),
                    "r": r, "g": g, "b": b,
                    "bright": round(bright, 1),
                })

    # Sort darkest-first so limited populations always hit the most defining pixels
    candidates.sort(key=lambda c: c["bright"])

    data = {"stamps": candidates, "src_w": w, "src_h": h}
    save_cache(cache_key, data)
    return jsonify(data)


# ── Page routes ──────────────────────────────────────────────────────────────

NAMES_CACHE_PATH = os.path.join(CACHE_DIR, "_names.json")

def _load_names_store():
    if os.path.exists(NAMES_CACHE_PATH):
        with open(NAMES_CACHE_PATH) as f:
            return json.load(f)
    return {}

def _save_names_store(store):
    with open(NAMES_CACHE_PATH, "w") as f:
        json.dump(store, f)

def _inaturalist_common_name(scientific_name):
    """Fetch English common name from iNaturalist for a single species."""
    try:
        r = requests.get(
            "https://api.inaturalist.org/v1/taxa",
            params={"q": scientific_name, "limit": 1},
            timeout=8,
        )
        results = r.json().get("results", [])
        if results:
            return results[0].get("preferred_common_name") or results[0].get("english_common_name")
    except Exception:
        pass
    return None

def _common_name_from_assessment_cache(assessment_id):
    cached = load_cache(f"assessment_{assessment_id}")
    if not cached:
        return None
    for n in cached.get("taxon", {}).get("common_names", []):
        if n.get("language") == "eng":
            return n["name"]
    return None

def enrich_common_names(assessments):
    """
    Add English common names to each item.
    1. Check the flat names store (persisted across requests)
    2. Check individual cached assessments
    3. Fetch missing ones from iNaturalist in parallel, then persist
    """
    store = _load_names_store()
    missing = []

    for item in assessments:
        aid = str(item["assessment_id"])
        if aid in store:
            item["common_name"] = store[aid]
        else:
            name = _common_name_from_assessment_cache(item["assessment_id"])
            if name:
                item["common_name"] = name
                store[aid] = name
            else:
                item["common_name"] = None
                missing.append(item)

    if missing:
        def fetch(item):
            name = _inaturalist_common_name(item["taxon_scientific_name"])
            return str(item["assessment_id"]), name

        with ThreadPoolExecutor(max_workers=10) as ex:
            futures = {ex.submit(fetch, item): item for item in missing}
            for future in as_completed(futures):
                aid, name = future.result()
                store[aid] = name
                # Update the item in place
                for item in missing:
                    if str(item["assessment_id"]) == aid:
                        item["common_name"] = name
                        break

        _save_names_store(store)

    return assessments


@app.route("/")
def index():
    category = request.args.get("category", "EX")
    available_cats = [c for c in CATEGORY_ORDER if c in _vertebrate_by_cat]
    if category not in _vertebrate_by_cat:
        category = available_cats[0] if available_cats else "EX"
    page = max(1, int(request.args.get("page", 1)))

    PAGE_SIZE = 100
    all_for_cat = _vertebrate_by_cat.get(category, [])
    start = (page - 1) * PAGE_SIZE
    assessments = all_for_cat[start:start + PAGE_SIZE]
    has_next = (start + PAGE_SIZE) < len(all_for_cat)

    return render_template(
        "index.html",
        assessments=assessments,
        category=category,
        category_label=CATEGORY_LABELS[category],
        categories=[(c, CATEGORY_LABELS[c]) for c in available_cats],
        page=page,
        has_next=has_next,
        total=len(all_for_cat),
    )


def _extract_trend(raw):
    if not raw:
        return None
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        desc = raw.get("description", {})
        return desc.get("en") or next(iter(desc.values()), None)
    return None


@app.route("/species/<int:assessment_id>")
def species(assessment_id):
    try:
        assessment = fetch_assessment(assessment_id)
    except Exception:
        abort(404)

    taxon = assessment.get("taxon", {})
    doc = assessment.get("documentation", {})
    supp = assessment.get("supplementary_info", {})

    common_names = [n["name"] for n in taxon.get("common_names", []) if n.get("language") == "eng"]
    common = common_names[0] if common_names else None

    summary = generate_summary(assessment)

    taxonomy = [
        ("Kingdom", taxon.get("kingdom_name", "").capitalize()),
        ("Phylum", taxon.get("phylum_name", "").capitalize()),
        ("Class", taxon.get("class_name", "").capitalize()),
        ("Order", taxon.get("order_name", "").capitalize()),
        ("Family", taxon.get("family_name", "").capitalize()),
        ("Genus", taxon.get("genus_name", "")),
    ]
    taxonomy = [(k, v) for k, v in taxonomy if v]

    docs = {}
    for key in ["range", "ecology", "threats", "conservation_actions", "rationale"]:
        val = strip_html(doc.get(key, ""))
        if val:
            docs[key.replace("_", " ").title()] = val

    category_code = assessment.get("red_list_category", {}).get("code", "")
    category_label = CATEGORY_LABELS.get(category_code, category_code)

    # Build a readable taxonomy string for Claude's prompt
    tax_parts = [
        taxon.get("class_name", "").capitalize(),
        taxon.get("order_name", "").capitalize(),
        taxon.get("family_name", "").capitalize(),
    ]
    taxonomy_str = " / ".join(p for p in tax_parts if p)

    scientific_name = taxon.get("scientific_name", "")
    art = _ensure_art(scientific_name, assessment_id, common, taxonomy_str)

    population = extract_population(assessment)

    return render_template(
        "species.html",
        assessment=assessment,
        taxon=taxon,
        common=common,
        summary=summary,
        taxonomy=taxonomy,
        docs=docs,
        category_code=category_code,
        category_label=category_label,
        year=assessment.get("year_published", ""),
        trend=_extract_trend(assessment.get("population_trend")),
        supp=supp,
        back_category=request.args.get("from", "EX"),
        has_art=art["has_sprite"],
        has_source_photo=art["has_image"],
        generating=art["generating"],
        population=population,
    )


@app.route("/species/<int:assessment_id>/particles")
def species_particles(assessment_id):
    try:
        assessment = fetch_assessment(assessment_id)
    except Exception:
        abort(404)

    taxon = assessment.get("taxon", {})
    common_names = [n["name"] for n in taxon.get("common_names", []) if n.get("language") == "eng"]
    common = common_names[0] if common_names else None

    category_code  = assessment.get("red_list_category", {}).get("code", "")
    category_label = CATEGORY_LABELS.get(category_code, category_code)
    animal_class   = taxon.get("class_name", "")

    taxonomy_str = " / ".join(
        p for p in [
            taxon.get("class_name", "").capitalize(),
            taxon.get("order_name", "").capitalize(),
            taxon.get("family_name", "").capitalize(),
        ] if p
    )

    art = _ensure_art(taxon.get("scientific_name", ""), assessment_id, common, taxonomy_str)
    population = extract_population(assessment)

    return render_template(
        "particles.html",
        assessment_id=assessment_id,
        name=common or taxon.get("scientific_name", ""),
        scientific_name=taxon.get("scientific_name", ""),
        category_code=category_code,
        category_label=category_label,
        animal_class=animal_class,
        has_image=art["has_image"],
        population=population,
    )


if __name__ == "__main__":
    app.run(debug=True, port=8080)
