"""Force-regenerate internet fallback images with real Stability AI art.
Targets CR mammals first, then others. Skips images already 800x800.
"""
import json, os, sys, time
from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.dirname(__file__))
import vectorize as vec

with open("cache/_vertebrates.json") as f:
    verts = json.load(f)

# Order: CR mammals first, then other mammals, then by category order
CAT_ORDER = ["CR", "EN", "EW", "EX", "VU", "NT", "LC"]
sorted_verts = sorted(verts, key=lambda v: (
    CAT_ORDER.index(v["category_code"]) if v["category_code"] in CAT_ORDER else 99,
    0 if v["class_name"] == "MAMMALIA" else 1,
))

to_regen = []
for sp in sorted_verts:
    aid = sp["assessment_id"]
    path = os.path.join(vec.IMG_DIR, f"{aid}.png")
    if not os.path.exists(path):
        to_regen.append((sp, "MISSING"))
        continue
    from PIL import Image
    try:
        img = Image.open(path)
        if img.size[0] != img.size[1]:
            to_regen.append((sp, "FALLBACK"))
    except Exception:
        to_regen.append((sp, "ERROR"))

print(f"Found {len(to_regen)} species needing Stability AI generation")
print(f"  (of {len(sorted_verts)} total vertebrates)")
print()

limit = int(sys.argv[1]) if len(sys.argv) > 1 else 50
print(f"Processing first {limit} species...\n")

ok, fail = 0, 0
for sp, reason in to_regen[:limit]:
    aid  = sp["assessment_id"]
    name = sp.get("common_name") or sp["scientific_name"]
    cat  = sp["category_code"]
    cls  = sp.get("class_name", "")

    # Delete fallback image so force=True actually overwrites
    path = os.path.join(vec.IMG_DIR, f"{aid}.png")
    if os.path.exists(path) and reason == "FALLBACK":
        os.remove(path)

    print(f"[{cat}] {name} ({aid}) [{reason}] ... ", end="", flush=True)
    img_path = vec.generate_stability_image(
        sp["scientific_name"], aid,
        common_name=sp.get("common_name"),
        animal_class=cls,
        force=True,
    )
    if img_path:
        from PIL import Image
        img = Image.open(img_path)
        print(f"OK {img.size[0]}x{img.size[1]}")
        ok += 1
    else:
        print("FAILED")
        fail += 1
    time.sleep(1.5)

print(f"\nDone. OK={ok} FAIL={fail}")
