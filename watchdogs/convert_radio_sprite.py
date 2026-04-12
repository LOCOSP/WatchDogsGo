"""Convert radio.png to pyxel-compatible 16-color sprite.

Resizes to target height (matching radar width ~40px) and maps colors
to pyxel's default palette. Black background becomes transparent (color 0).
"""

from pathlib import Path
from PIL import Image
import math

# Pyxel default palette (RGB)
PYXEL_PALETTE = [
    (0, 0, 0),        # 0  black (transparent)
    (43, 51, 95),      # 1  dark blue
    (126, 32, 114),    # 2  purple
    (25, 149, 156),    # 3  cyan
    (139, 72, 82),     # 4  brown
    (57, 92, 152),     # 5  slate blue
    (169, 193, 255),   # 6  light blue
    (238, 238, 238),   # 7  white
    (212, 24, 108),    # 8  red/pink
    (211, 132, 65),    # 9  orange
    (233, 195, 91),    # 10 yellow
    (112, 198, 169),   # 11 green
    (118, 150, 222),   # 12 blue
    (163, 163, 163),   # 13 grey
    (255, 151, 152),   # 14 light pink
    (237, 180, 161),   # 15 peach
]


def _nearest_color(r, g, b):
    """Find nearest pyxel palette color (skip 0=black for dark pixels)."""
    # Very dark pixels → black (transparent)
    if r < 20 and g < 20 and b < 20:
        return 0
    best_idx = 1
    best_dist = float('inf')
    for i, (pr, pg, pb) in enumerate(PYXEL_PALETTE):
        if i == 0:
            continue  # skip black/transparent
        dist = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2
        if dist < best_dist:
            best_dist = dist
            best_idx = i
    return best_idx


def convert(src_path: str, dst_path: str, target_h: int = 48) -> None:
    """Convert source PNG to pyxel sprite."""
    img = Image.open(src_path).convert("RGBA")

    # Crop to content (remove black border)
    bbox = img.getbbox()
    if bbox:
        img = img.crop(bbox)

    # Resize maintaining aspect ratio
    w, h = img.size
    scale = target_h / h
    new_w = max(1, round(w * scale))
    new_h = target_h
    img = img.resize((new_w, new_h), Image.NEAREST)

    # Map to pyxel palette
    out = Image.new("RGB", (new_w, new_h), (0, 0, 0))
    src_px = img.load()
    dst_px = out.load()

    for y in range(new_h):
        for x in range(new_w):
            px = src_px[x, y]
            r, g, b = px[0], px[1], px[2]
            a = px[3] if len(px) > 3 else 255
            if a < 128:
                dst_px[x, y] = (0, 0, 0)  # transparent
            else:
                idx = _nearest_color(r, g, b)
                dst_px[x, y] = PYXEL_PALETTE[idx]

    out.save(dst_path)


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent / "assets"
    convert(str(root / "radio.png"), str(root / "radio_sprite.png"), 48)
    print(f"Saved {root / 'radio_sprite.png'}")
