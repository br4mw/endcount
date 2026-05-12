"""
Image and sprite pipeline.

Source photo  : iNaturalist → Wikipedia fallback → cached PNG
SVG sprite    : Claude generates an ink illustration from style examples + species knowledge
Sprite PNG    : SVG rasterized to colour-on-transparent via cairosvg
"""

import base64
import io
import os
import re

import anthropic
import cairosvg
import requests
from dotenv import load_dotenv
from PIL import Image

load_dotenv()

CACHE_DIR    = os.path.join(os.path.dirname(__file__), "cache")
IMG_DIR      = os.path.join(CACHE_DIR, "images")
SVG_DIR      = os.path.join(CACHE_DIR, "sprites_svg")
PNG_DIR      = os.path.join(CACHE_DIR, "sprites_png")
EXAMPLES_DIR = os.path.join(os.path.dirname(__file__), "spriteexamples")

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


# ── Style reference images ────────────────────────────────────────────────────

_EXAMPLE_IMAGES = None  # cached at module level

def _get_example_images():
    """Load style reference images from spriteexamples/ as base64 blocks for Claude vision."""
    global _EXAMPLE_IMAGES
    if _EXAMPLE_IMAGES is not None:
        return _EXAMPLE_IMAGES

    _EXAMPLE_IMAGES = []
    if not os.path.isdir(EXAMPLES_DIR):
        return _EXAMPLE_IMAGES

    ext_to_mime = {
        ".webp": "image/webp",
        ".png":  "image/png",
        ".jpg":  "image/jpeg",
        ".jpeg": "image/jpeg",
    }
    for fname in sorted(os.listdir(EXAMPLES_DIR)):
        ext = os.path.splitext(fname)[1].lower()
        if ext not in ext_to_mime:
            continue
        with open(os.path.join(EXAMPLES_DIR, fname), "rb") as f:
            data = base64.standard_b64encode(f.read()).decode()
        _EXAMPLE_IMAGES.append({
            "type": "image",
            "source": {"type": "base64", "media_type": ext_to_mime[ext], "data": data},
        })

    return _EXAMPLE_IMAGES


# ── Claude SVG generation ─────────────────────────────────────────────────────

SVG_PROMPT = """The images above show the illustration style I want you to match exactly.

Key characteristics of that style:
- Bold expressive black ink outlines (stroke-based, not solid fills) — confident, slightly loose line quality
- Interior anatomical detail lines (thinner strokes) showing musculature, scale texture, fin rays, feather groups, bone structure
- Flat natural colour fills placed behind the ink lines using z-order (the creature's real colours — skin, scales, plumage, fur)
- A dynamic, living pose (profile or three-quarter) that captures the creature's character and movement
- No watercolour background, no labels, no decorative elements

Now create an SVG illustration of {common_name} (scientific name: {scientific_name}), a {taxonomy}, in exactly this style.

SVG requirements:
- viewBox="0 0 200 200", no width/height attributes
- Colour fill shapes first (behind), then ink outline paths on top
- Outer body outline: stroke="#111111" stroke-width="2.5" fill="none"
- Interior detail lines: stroke="#111111" stroke-width="1" fill="none"
- Fill colours: use the creature's natural colours as flat fills (realistic hues, not black)
- The animal should fill most of the viewBox with a small margin
- No background rect, no white fill, no text
- Return ONLY the raw SVG starting with <svg and ending with </svg>"""


def generate_svg_with_claude(scientific_name, common_name, taxonomy, assessment_id, force=False):
    """
    Ask Claude to draw the species as a styled ink illustration SVG.
    Returns path to saved SVG, or None on failure.
    Set force=True to overwrite an existing file.
    """
    svg_path = os.path.join(SVG_DIR, f"{assessment_id}.svg")
    if os.path.exists(svg_path) and not force:
        return svg_path

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None

    prompt_text = SVG_PROMPT.format(
        common_name=common_name or scientific_name,
        scientific_name=scientific_name,
        taxonomy=taxonomy,
    )

    # Build message content: style reference images + text prompt
    content = _get_example_images() + [{"type": "text", "text": prompt_text}]

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            messages=[{"role": "user", "content": content}],
        )
        raw = msg.content[0].text.strip()

        match = re.search(r"<svg[\s\S]*?</svg>", raw, re.IGNORECASE)
        if not match:
            return None

        svg_content = match.group(0)
        if "xmlns" not in svg_content:
            svg_content = svg_content.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1)

        with open(svg_path, "w") as f:
            f.write(svg_content)

        return svg_path

    except Exception:
        return None


# ── SVG → colour-on-transparent PNG ──────────────────────────────────────────

def make_sprite_png(assessment_id, force=False):
    """
    Rasterize the SVG to a colour-on-transparent PNG.
    Near-white background pixels are made transparent so the artwork sits
    cleanly on any background colour.
    """
    png_path = os.path.join(PNG_DIR, f"{assessment_id}.png")
    if os.path.exists(png_path) and not force:
        return png_path

    svg_path = os.path.join(SVG_DIR, f"{assessment_id}.svg")
    if not os.path.exists(svg_path):
        return None

    try:
        with open(svg_path) as f:
            svg_content = f.read()

        png_bytes = cairosvg.svg2png(
            bytestring=svg_content.encode(),
            output_width=400,
            output_height=400,
        )

        img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
        pixels = img.load()
        w, h = img.size

        # Make near-white pixels transparent (SVG background bleed)
        for y in range(h):
            for x in range(w):
                r, g, b, a = pixels[x, y]
                if r > 238 and g > 238 and b > 238:
                    pixels[x, y] = (r, g, b, 0)

        img.save(png_path, "PNG")
        return png_path

    except Exception:
        return None


# ── Orchestrator ──────────────────────────────────────────────────────────────

def art_status(assessment_id):
    """Fast check of which art files exist — no generation, no API calls."""
    has_image  = os.path.exists(os.path.join(IMG_DIR, f"{assessment_id}.png"))
    has_sprite = os.path.exists(os.path.join(PNG_DIR, f"{assessment_id}.png"))
    return {"has_image": has_image, "has_sprite": has_sprite}


def prepare_species_art(scientific_name, assessment_id, common_name=None, taxonomy=None, force=False):
    """
    Full generation pipeline — intended to be called from a background thread.
    Set force=True to regenerate even if files already exist.
    Returns: { has_image, has_sprite, source_path, svg_path, sprite_png_path }
    """
    result = {
        "has_image":      False,
        "has_sprite":     False,
        "source_path":    None,
        "svg_path":       None,
        "sprite_png_path": None,
    }

    url = fetch_image_url(scientific_name)
    if url:
        source_path = download_source(url, assessment_id)
        if source_path:
            result["has_image"]   = True
            result["source_path"] = source_path

    svg_path = generate_svg_with_claude(
        scientific_name,
        common_name or scientific_name,
        taxonomy or "animal",
        assessment_id,
        force=force,
    )
    if svg_path:
        result["has_sprite"]     = True
        result["svg_path"]       = svg_path
        result["sprite_png_path"] = make_sprite_png(assessment_id, force=force)

    return result
