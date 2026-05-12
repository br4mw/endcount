"""
Image and sprite pipeline.

Stability AI  : Generates the primary Audubon-style illustration (background + particle source)
Claude SVG    : Generates a simple white-outline icon sprite for particle texture
Particle data : Samples the illustration for particle positions, colours, and 3D depth
"""

import base64
import io
import json
import os
import re

import anthropic
import cairosvg
import requests
from dotenv import load_dotenv
from PIL import Image

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

CACHE_DIR    = os.path.join(os.path.dirname(__file__), "cache")
IMG_DIR      = os.path.join(CACHE_DIR, "images")
SVG_DIR      = os.path.join(CACHE_DIR, "sprites_svg")
PNG_DIR      = os.path.join(CACHE_DIR, "sprites_png")

for d in (IMG_DIR, SVG_DIR, PNG_DIR):
    os.makedirs(d, exist_ok=True)


# ── Stability AI illustration ─────────────────────────────────────────────────

STABILITY_BASE = "https://api.stability.ai/v2beta/stable-image/generate/core"

STABILITY_PROMPT = (
    "John James Audubon naturalist plate illustration of a single adult {common_name} "
    "({scientific_name}), a {taxonomy}. One animal only — no other animals, no young. "
    "Complete animal fully within the frame, nothing cut off at the edges. "
    "Full body visible: head to tail tip, all limbs and extremities intact. "
    "Detailed {feature}, naturalistic colours, dramatic three-quarter pose. "
    "Plain white background only — no ground, no shadow, no rocks, no plants, "
    "no water, no environmental context of any kind. "
    "Fine ink linework, 19th century natural history plate style, museum quality."
)

# Anatomy feature hint by broad class
_FEATURE_HINTS = {
    "aves":           "feather groups and wing structure",
    "actinopterygii": "scales, fin rays, and lateral line",
    "chondrichthyes": "cartilaginous fins and skin texture",
    "mammalia":       "fur direction and musculature",
    "reptilia":       "scale rows and skin texture",
    "amphibia":       "smooth skin and limb structure",
    "insecta":        "wing venation and segmentation",
    "arachnida":      "leg joints and body segments",
}

def _feature_for_class(animal_class):
    cls = animal_class.lower()
    for key, hint in _FEATURE_HINTS.items():
        if key in cls:
            return hint
    return "surface texture and body structure"


def generate_stability_image(scientific_name, assessment_id,
                              common_name=None, animal_class="", force=False):
    """
    Generate an Audubon-style illustration via Stability AI.
    Stores result in cache/images/<id>.png.
    Returns path or None.
    """
    path = os.path.join(IMG_DIR, f"{assessment_id}.png")
    if os.path.exists(path) and not force:
        return path

    api_key = os.environ.get("STABILITY_API_KEY", "")
    if not api_key:
        return None

    prompt = STABILITY_PROMPT.format(
        common_name=common_name or scientific_name,
        scientific_name=scientific_name,
        taxonomy=animal_class or "animal",
        feature=_feature_for_class(animal_class),
    )

    try:
        r = requests.post(
            STABILITY_BASE,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "image/*"},
            files={"none": ""},
            data={"prompt": prompt, "aspect_ratio": "1:1", "output_format": "png"},
            timeout=30,
        )
        r.raise_for_status()
        raw_png = r.content

        # Strip background — always composite onto white
        r2 = requests.post(
            "https://api.stability.ai/v2beta/stable-image/edit/remove-background",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "image/*"},
            files={"image": ("image.png", raw_png, "image/png")},
            data={"output_format": "png"},
            timeout=30,
        )
        if r2.status_code == 200:
            fg = Image.open(io.BytesIO(r2.content)).convert("RGBA")
            bg = Image.new("RGBA", fg.size, (255, 255, 255, 255))
            bg.paste(fg, mask=fg.split()[3])
            img = bg.convert("RGB")
        else:
            img = Image.open(io.BytesIO(raw_png)).convert("RGB")

        img.thumbnail((800, 800), Image.LANCZOS)
        img.save(path, "PNG")
        return path
    except Exception:
        return None




# ── Particle data sampling ────────────────────────────────────────────────────

def sample_particle_data(assessment_id, target_count=2800, force=False):
    """
    Sample the source illustration for particle home positions, colours, and depth.
    Returns path to cached JSON, or None.

    JSON schema:
      { "particles": [{x, y, z, r, g, b}, ...] }

    x, y : normalised [-1, 1] in Three.js world space (mapped to ±300 units at call site)
    z    : depth offset in world units — darker pixels sit slightly forward
    r,g,b: 0-255 colour from the illustration
    """
    cache_path = os.path.join(CACHE_DIR, f"particle_data_{assessment_id}.json")
    if os.path.exists(cache_path) and not force:
        return cache_path

    img_path = os.path.join(IMG_DIR, f"{assessment_id}.png")
    if not os.path.exists(img_path):
        return None

    img = Image.open(img_path).convert("RGB")
    w, h = img.size
    pixels = img.load()

    # Step size so we sample ≈ target_count non-background pixels
    step = max(1, int((w * h / (target_count * 2)) ** 0.5))

    candidates = []
    for y in range(0, h, step):
        for x in range(0, w, step):
            r, g, b = pixels[x, y]
            lum = (r + g + b) / 3
            if lum > 242:          # skip white background
                continue
            nx  =  (x / w - 0.5) * 2         # -1 → 1 left to right
            ny  = -(y / h - 0.5) * 2         # -1 → 1 bottom to top
            # Depth: darker (ink lines, shadows) come forward; light fills go back
            nz  = (1 - lum / 255) * 60       # 0–60 world units
            candidates.append({
                "x": round(nx, 4),
                "y": round(ny, 4),
                "z": round(nz, 2),
                "r": r, "g": g, "b": b,
            })

    # Trim / pad to target_count
    import random
    if len(candidates) > target_count:
        # Keep darkest pixels (most defining) plus a random sample of the rest
        candidates.sort(key=lambda p: (p["r"] + p["g"] + p["b"]))
        keep = candidates[:target_count // 2]
        rest = candidates[target_count // 2:]
        random.shuffle(rest)
        candidates = keep + rest[:target_count - len(keep)]

    random.shuffle(candidates)  # randomise order so staggered release looks organic

    data = {"particles": candidates}
    with open(cache_path, "w") as f:
        json.dump(data, f)
    return cache_path


# ── Sprite SVG: edge detection → potrace ─────────────────────────────────────

def generate_svg_with_claude(scientific_name, common_name, taxonomy, assessment_id, force=False):
    """
    Generate a white-outline sprite SVG from the artistic render.
    Pipeline: grayscale → edge detect → dilate → potrace → white stroke SVG.
    The clean white background of the render means edges are purely from the animal.
    """
    svg_path = os.path.join(SVG_DIR, f"{assessment_id}.svg")
    if os.path.exists(svg_path) and not force:
        return svg_path

    img_path = os.path.join(IMG_DIR, f"{assessment_id}.png")
    if not os.path.exists(img_path):
        return None

    try:
        import cv2, subprocess, tempfile, xml.etree.ElementTree as ET
        import numpy as np

        img = cv2.imread(img_path)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        # Mask: non-white pixels = the animal
        _, animal_mask = cv2.threshold(gray, 245, 255, cv2.THRESH_BINARY_INV)
        animal_mask = cv2.morphologyEx(animal_mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))

        # Edge detection inside the animal only — captures outline + internal structure
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blurred, 120, 260)
        edges = cv2.bitwise_and(edges, animal_mask)

        # Dilate just enough to connect broken edge segments
        edges = cv2.dilate(edges, np.ones((2, 2), np.uint8), iterations=1)

        # potrace needs: black pixels (0) = trace, white (255) = background
        # edges image: 255 = edge → we want those as black for potrace
        pbm_data = 255 - edges  # invert: edges become black

        with tempfile.TemporaryDirectory() as tmp:
            pbm_path = os.path.join(tmp, "edges.pbm")
            svg_tmp  = os.path.join(tmp, "out.svg")
            Image.fromarray(pbm_data).convert("1").save(pbm_path)

            r = subprocess.run(
                ["potrace", pbm_path, "-s", "-o", svg_tmp,
                 "--opttolerance", "0.8",
                 "-t", "14"],
                capture_output=True,
            )
            if r.returncode != 0:
                return None

            with open(svg_tmp) as f:
                raw_svg = f.read()

        root = ET.fromstring(raw_svg)
        vb = root.get("viewBox", "").split()
        vb_h = float(vb[3]) if len(vb) >= 4 else gray.shape[0]

        paths_d = [e.get("d", "") for e in root.iter() if e.tag.endswith("path") and e.get("d")]
        if not paths_d:
            return None

        sx = 100.0 / (vb_h * 10.0)
        sw = 0.6 / sx  # ~0.6px stroke in 100×100 space

        path_els = "\n".join(
            f'  <path d="{d}" fill="none" stroke="rgba(255,255,255,0.9)" '
            f'stroke-width="{sw:.2f}" stroke-linecap="round" stroke-linejoin="round"/>'
            for d in paths_d
        )

        svg_content = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">\n'
            f'  <g transform="translate(0,100) scale({sx:.6f},{-sx:.6f})">\n'
            f'{path_els}\n'
            '  </g>\n</svg>'
        )

        with open(svg_path, "w") as f:
            f.write(svg_content)
        return svg_path

    except Exception:
        return None


# ── SVG → sprite PNG (white outline on transparent) ──────────────────────────

def make_sprite_png(assessment_id, force=False):
    """
    Rasterize the outline SVG to a 200×200 white-on-transparent PNG.
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
            output_width=200,
            output_height=200,
            background_color="transparent",
        )
        img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
        img.save(png_path, "PNG")
        return png_path

    except Exception:
        return None


# ── Status + orchestrator ─────────────────────────────────────────────────────

def art_status(assessment_id):
    """Fast check — no generation, no API calls."""
    return {
        "has_image":  os.path.exists(os.path.join(IMG_DIR,  f"{assessment_id}.png")),
        "has_sprite": os.path.exists(os.path.join(PNG_DIR,  f"{assessment_id}.png")),
        "has_particle_data": os.path.exists(
            os.path.join(CACHE_DIR, f"particle_data_{assessment_id}.json")),
    }


def prepare_species_art(scientific_name, assessment_id,
                        common_name=None, taxonomy=None,
                        animal_class="", force=False):
    """
    Full pipeline (run from a background thread).
    1. Generate Stability AI illustration → source image + particle data
    2. Generate Claude outline SVG → sprite PNG
    """
    result = {
        "has_image": False, "has_sprite": False,
        "has_particle_data": False,
    }

    # 1. Stability AI illustration
    img_path = generate_stability_image(
        scientific_name, assessment_id,
        common_name=common_name,
        animal_class=animal_class or taxonomy or "",
        force=force,
    )
    if not img_path:
        # Fallback: iNaturalist / Wikipedia photo
        img_path = _fetch_fallback_image(scientific_name, assessment_id)

    if img_path:
        result["has_image"] = True
        pdata = sample_particle_data(assessment_id, force=force)
        result["has_particle_data"] = bool(pdata)

    # 2. Claude outline SVG → sprite PNG
    svg_path = generate_svg_with_claude(
        scientific_name,
        common_name or scientific_name,
        taxonomy or animal_class or "animal",
        assessment_id,
        force=force,
    )
    if svg_path:
        png = make_sprite_png(assessment_id, force=force)
        result["has_sprite"] = bool(png)

    return result


def _fetch_fallback_image(scientific_name, assessment_id):
    """iNaturalist → Wikipedia fallback if Stability AI key is absent."""
    path = os.path.join(IMG_DIR, f"{assessment_id}.png")
    if os.path.exists(path):
        return path
    for url in (_inaturalist_url(scientific_name), _wikipedia_url(scientific_name)):
        if not url:
            continue
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            img = Image.open(io.BytesIO(r.content)).convert("RGB")
            img.thumbnail((800, 800), Image.LANCZOS)
            img.save(path, "PNG")
            return path
        except Exception:
            continue
    return None


def _inaturalist_url(scientific_name):
    try:
        r = requests.get("https://api.inaturalist.org/v1/taxa",
                         params={"q": scientific_name, "limit": 1}, timeout=10)
        results = r.json().get("results", [])
        if results and results[0].get("default_photo"):
            return results[0]["default_photo"].get("medium_url")
    except Exception:
        pass
    return None


def _wikipedia_url(scientific_name):
    try:
        r = requests.get(
            f"https://en.wikipedia.org/api/rest_v1/page/summary/{scientific_name.replace(' ', '_')}",
            timeout=10)
        data = r.json()
        t = data.get("thumbnail") or data.get("originalimage")
        if t:
            return t.get("source")
    except Exception:
        pass
    return None
