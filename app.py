import os
import json
import re
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
    if category not in CATEGORY_LABELS:
        category = "EX"
    page = max(1, int(request.args.get("page", 1)))

    data = fetch_species_list(category, page)
    assessments = data.get("assessments", [])
    has_next = len(assessments) == 100

    enrich_common_names(assessments)

    return render_template(
        "index.html",
        assessments=assessments,
        category=category,
        category_label=CATEGORY_LABELS[category],
        categories=[(c, CATEGORY_LABELS[c]) for c in CATEGORY_ORDER],
        page=page,
        has_next=has_next,
    )


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

    # Prepare art assets (runs pipeline on first visit, then cached)
    scientific_name = taxon.get("scientific_name", "")
    art = vec.prepare_species_art(
        scientific_name, assessment_id,
        common_name=common,
        taxonomy=taxonomy_str,
    )

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
        trend=assessment.get("population_trend"),
        supp=supp,
        back_category=request.args.get("from", "EX"),
        has_art=art["has_sprite"],
        has_source_photo=art["has_image"],
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

    art = vec.prepare_species_art(
        taxon.get("scientific_name", ""), assessment_id,
        common_name=common,
        taxonomy=taxonomy_str,
    )

    return render_template(
        "particles.html",
        assessment_id=assessment_id,
        name=common or taxon.get("scientific_name", ""),
        scientific_name=taxon.get("scientific_name", ""),
        category_code=category_code,
        category_label=category_label,
        animal_class=animal_class,
        has_image=art["has_image"],
    )


if __name__ == "__main__":
    app.run(debug=True, port=8080)
