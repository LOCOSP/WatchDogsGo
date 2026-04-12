"""Convert hacker character PNG to pyxel-optimized sprite.

Usage: python3 -m watchdogs.convert_sprite <input_image> [output_png]

Takes a high-res character image (on black background) and creates a
256-pixel-tall PNG optimized for pyxel's 16-color palette.
Pyxel will load this directly with image().load().
"""

import sys
from pathlib import Path


PYXEL_PALETTE_RGB = [
    (0x00, 0x00, 0x00),  # 0  black (transparent)
    (0x2B, 0x33, 0x5F),  # 1  dark blue
    (0x7E, 0x20, 0x72),  # 2  purple
    (0x19, 0x95, 0x9C),  # 3  teal
    (0x8B, 0x48, 0x52),  # 4  brown
    (0x39, 0x5C, 0x98),  # 5  blue
    (0xA9, 0xC1, 0xFF),  # 6  light blue
    (0xEE, 0xEE, 0xEE),  # 7  white
    (0xD4, 0x18, 0x6C),  # 8  pink/red
    (0xD3, 0x84, 0x41),  # 9  orange
    (0xE9, 0xC3, 0x5B),  # 10 yellow
    (0x70, 0xC6, 0xA9),  # 11 green
    (0x76, 0x96, 0xDE),  # 12 medium blue
    (0xA3, 0xA3, 0xA3),  # 13 gray
    (0xFF, 0x97, 0x98),  # 14 light pink
    (0xED, 0xB4, 0xA1),  # 15 peach
]


def nearest_palette(r, g, b):
    """Find nearest pyxel palette color."""
    best_i, best_d = 0, 999999
    for i, (pr, pg, pb) in enumerate(PYXEL_PALETTE_RGB):
        d = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2
        if d < best_d:
            best_d = d
            best_i = i
    return best_i


def convert(input_path: str, output_path: str = None):
    """Convert input image to pyxel-palette PNG."""
    from PIL import Image, ImageEnhance

    if output_path is None:
        output_path = str(Path(__file__).parent.parent / "assets" / "hacker_sprite.png")

    img = Image.open(input_path).convert("RGB")
    print(f"Input: {img.size[0]}x{img.size[1]}")

    # Resize to fit in single 256x256 pyxel image bank
    # Legs extend below terminal for cinematic effect
    target_h = 256
    ratio = target_h / img.size[1]
    target_w = int(img.size[0] * ratio)
    if target_w > 256:
        target_w = 256
        ratio = target_w / img.size[0]
        target_h = int(img.size[1] * ratio)

    img = img.resize((target_w, target_h), Image.LANCZOS)
    print(f"Resized: {target_w}x{target_h}")

    # Boost contrast and brightness slightly for dark image
    img = ImageEnhance.Contrast(img).enhance(1.3)
    img = ImageEnhance.Brightness(img).enhance(1.2)

    # Convert to pyxel palette
    pixels = img.load()
    lut = {}
    for y in range(target_h):
        for x in range(target_w):
            rgb = pixels[x, y]
            if rgb not in lut:
                r, g, b = rgb
                brightness = (r + g + b) // 3
                if brightness < 18:
                    # Near-black → palette 0 (will be transparent in pyxel)
                    lut[rgb] = 0
                else:
                    lut[rgb] = nearest_palette(r, g, b)
            idx = lut[rgb]
            pr, pg, pb = PYXEL_PALETTE_RGB[idx]
            pixels[x, y] = (pr, pg, pb)

    # Save as PNG (pyxel can load this directly)
    img.save(output_path)
    print(f"Output: {output_path} ({target_w}x{target_h}, {len(lut)} unique colors)")

    # Print color distribution
    counts = {}
    for y in range(target_h):
        for x in range(target_w):
            c = pixels[x, y]
            counts[c] = counts.get(c, 0) + 1
    for c, n in sorted(counts.items(), key=lambda x: -x[1])[:8]:
        idx = PYXEL_PALETTE_RGB.index(c) if c in PYXEL_PALETTE_RGB else "?"
        pct = n / (target_w * target_h) * 100
        print(f"  [{idx}] {c} → {pct:.1f}%")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 -m watchdogs.convert_sprite <input_image> [output_png]")
        sys.exit(1)
    convert(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
