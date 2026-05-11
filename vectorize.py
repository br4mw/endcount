"""
Image and sprite pipeline.

Source photo  : iNaturalist → Wikipedia fallback → cached PNG
SVG sprite    : Claude generates from species knowledge (not photo tracing)
Stamp PNG     : SVG rasterized to white-on-transparent via cairosvg
"""

import io
import os
import re

import anthropic
import cairosvg
import requests
from dotenv import load_dotenv
from PIL import Image

load_dotenv()

CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
IMG_DIR    = os.path.join(CACHE_DIR, "images")
SVG_DIR    = os.path.join(CACHE_DIR, "sprites_svg")
PNG_DIR    = os.path.join(CACHE_DIR, "sprites_png")

for d in (IMG_DIR, SVG_DIR, PNG_DIR):
    os.makedirs(d, exist_ok=True)


# ── Image fetching ───────────────────────────────────────────────────────────

def _inaturalist_url(scientific_name):
    try:
        r = requests.get(
            "https://api.inaturalist.org/v1/taxa",
            params={"q": scientific_name, "limit": 1},
            timeout=10,
        )
        results = r.json().get("results", [])
        if results and results[0].get("default_photo"):
            return results[0]["default_photo"].get("medium_url")
    except Exception:
        pass
    return None


def _wikipedia_url(scientific_name):
    try:
        title = scientific_name.replace(" ", "_")
        r = requests.get(
            f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}",
            timeout=10,
        )
        data = r.json()
        thumbnail = data.get("thumbnail") or data.get("originalimage")
        if thumbnail:
            return thumbnail.get("source")
    except Exception:
        pass
    return None


def fetch_image_url(scientific_name):
    return _inaturalist_url(scientific_name) or _wikipedia_url(scientific_name)


def download_source(url, assessment_id):
    path = os.path.join(IMG_DIR, f"{assessment_id}.png")
    if os.path.exists(path):
        return path
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content)).convert("RGB")
        img.thumbnail((800, 800), Image.LANCZOS)
        img.save(path, "PNG")
        return path
    except Exception:
        return None


# ── Claude SVG generation ────────────────────────────────────────────────────

SVG_PROMPT = """Create a minimal SVG silhouette of {common_name} (scientific name: {scientific_name}), a {taxonomy}.

Requirements:
- viewBox="0 0 200 200"
- Single solid black silhouette, fill="#000000", no stroke
- No background rect, no text, no labels, no decorative elements
- Natural side-profile or three-quarter pose that best shows the animal's distinctive shape
- The silhouette should fill most of the viewBox with generous margin
- Clean, smooth paths — this will be stamped at small sizes (20–60px) so it must read clearly
- Capture the animal's most recognisable features (body shape, distinctive markings outline, characteristic posture)
- Return ONLY the raw SVG code starting with <svg and ending with </svg>"""


def generate_svg_with_claude(scientific_name, common_name, taxonomy, assessment_id):
    """
    Ask Claude to draw the species as a vector silhouette.
    Returns path to saved SVG, or None on failure.
    """
    svg_path = os.path.join(SVG_DIR, f"{assessment_id}.svg")
    if os.path.exists(svg_path):
        return svg_path

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None

    prompt = SVG_PROMPT.format(
        common_name=common_name or scientific_name,
        scientific_name=scientific_name,
        taxonomy=taxonomy,
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()

        # Extract SVG block
        match = re.search(r"<svg[\s\S]*?</svg>", raw, re.IGNORECASE)
        if not match:
            return None

        svg_content = match.group(0)

        # Ensure xmlns present
        if "xmlns" not in svg_content:
            svg_content = svg_content.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1)

        with open(svg_path, "w") as f:
            f.write(svg_content)

        return svg_path

    except Exception:
        return None


# ── SVG → white-on-transparent PNG ──────────────────────────────────────────

def make_sprite_png(assessment_id):
    """
    Rasterize the Claude SVG to a white-on-transparent PNG for p5.js tinting.
    White subject = tint() can freely colorise each stamp.
    """
    png_path = os.path.join(PNG_DIR, f"{assessment_id}.png")
    if os.path.exists(png_path):
        return png_path

    svg_path = os.path.join(SVG_DIR, f"{assessment_id}.svg")
    if not os.path.exists(svg_path):
        return None

    try:
        with open(svg_path) as f:
            svg_content = f.read()

        # Rasterize at 400×400
        png_bytes = cairosvg.svg2png(
            bytestring=svg_content.encode(),
            output_width=400,
            output_height=400,
        )

        # The SVG is black on transparent.
        # Invert: make the silhouette white, keep alpha.
        img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
        r, g, b, a = img.split()

        # Where the SVG drew black (dark pixels), flip to white.
        # Use the alpha channel as mask — SVG fills are opaque.
        white = Image.new("L", img.size, 255)
        result = Image.merge("RGBA", (white, white, white, a))
        result.save(png_path, "PNG")
        return png_path

    except Exception:
        return None


# ── Orchestrator ─────────────────────────────────────────────────────────────

def art_status(assessment_id):
    """Fast check of which art files exist — no generation, no API calls."""
    has_image  = os.path.exists(os.path.join(IMG_DIR, f"{assessment_id}.png"))
    has_sprite = os.path.exists(os.path.join(PNG_DIR, f"{assessment_id}.png"))
    return {"has_image": has_image, "has_sprite": has_sprite}


def prepare_species_art(scientific_name, assessment_id, common_name=None, taxonomy=None):
    """
    Full generation pipeline — intended to be called from a background thread.
    All steps are idempotent: files are only written if they don't already exist.
    Returns: { has_image, has_sprite, source_path, svg_path, sprite_png_path }
    """
    result = {
        "has_image": False,
        "has_sprite": False,
        "source_path": None,
        "svg_path": None,
        "sprite_png_path": None,
    }

    # Source photo (for pixel sampling)
    url = fetch_image_url(scientific_name)
    if url:
        source_path = download_source(url, assessment_id)
        if source_path:
            result["has_image"] = True
            result["source_path"] = source_path

    # Claude SVG sprite (independent of photo)
    svg_path = generate_svg_with_claude(
        scientific_name,
        common_name or scientific_name,
        taxonomy or "animal",
        assessment_id,
    )
    if svg_path:
        result["has_sprite"] = True
        result["svg_path"] = svg_path
        result["sprite_png_path"] = make_sprite_png(assessment_id)

    return result
