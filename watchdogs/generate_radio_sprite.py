"""Generate a pixel-art CB radio sprite for MeshCore notifications.

Creates a 32x48 pixel walkie-talkie image using pyxel's 16-color palette.
Saved to assets/radio_sprite.png for loading into pyxel image bank 2.
"""

from pathlib import Path

# Pyxel default palette (RGB)
PYXEL_PALETTE = [
    (0, 0, 0),        # 0  black
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

# 32x48 pixel art walkie-talkie (color indices)
# 0=transparent(black), 1=dark, 5=body dark, 13=body grey, 7=highlight
# 3=cyan screen, 11=green signal, 10=yellow buttons, 9=orange accent
# fmt: off
RADIO = [
    # Antenna tip (rows 0-3)
    "00000000000000008800000000000000",
    "00000000000000018100000000000000",
    "00000000000000018100000000000000",
    "00000000000000018100000000000000",
    # Antenna base (rows 4-7)
    "00000000000000018100000000000000",
    "00000000000000118110000000000000",
    "00000000000001111111000000000000",
    "00000000000001111111000000000000",
    # Top panel - knobs (rows 8-11)
    "00000000001111111111111000000000",
    "000000000B111DD11DD111B000000000",
    "000000000B11199119911AB000000000",
    "00000000011119911991111000000000",
    # Body top edge (rows 12-15)
    "0000000011111111111111110000000D",
    "000000015555555555555555100000DD",
    "00000001555DDDDDDDDDD555100000D",
    "00000001555DDDDDDDDDD555100000D",
    # Screen area (rows 16-23)
    "0000000155511111111115551000000D",
    "0000000155510000000015551000000D",
    "000000015551003B300015551000000D",
    "00000001555100BBB00015551000000D",
    "0000000155510B3B3B0015551000000D",
    "000000015551003B300015551000000D",
    "0000000155510000000015551000000D",
    "0000000155511111111115551000000D",
    # Mid body - speaker (rows 24-31)
    "00000001555DDDDDDDDDD555100000D",
    "0000000155500033300005551000000D",
    "00000001555DDDDDDDDDD555100000D",
    "0000000155500033300005551000000D",
    "00000001555DDDDDDDDDD555100000D",
    "0000000155500033300005551000000D",
    "00000001555DDDDDDDDDD555100000D",
    "00000001555DDDDDDDDDD555100000D",
    # Button panel (rows 32-39)
    "00000001555DDDDDDDDDD555100000D",
    "000000015551AA1AA1AA15551000000D",
    "000000015551AA1AA1AA15551000000D",
    "00000001555DDDDDDDDDD555100000D",
    "000000015551AA1AA1AA15551000000D",
    "000000015551AA1AA1AA15551000000D",
    "00000001555DDDDDDDDDD555100000D",
    "000000015551AA18815551551000000D",
    # Bottom (rows 40-47)
    "00000001555DDDDDDDDDD555100000D",
    "00000001555DDDDDDDDDD555100000D",
    "00000001555555555555555551000000",
    "00000001111111111111111111000000",
    "00000000011111111111111100000000",
    "00000000000000000000000000000000",
    "00000000000000000000000000000000",
    "00000000000000000000000000000000",
]
# fmt: on

# Map hex chars to palette indices
_CHAR_MAP = {
    '0': None,  # transparent
    '1': 1, '2': 2, '3': 3, '4': 4, '5': 5,
    '6': 6, '7': 7, '8': 8, '9': 9,
    'A': 10, 'B': 11, 'C': 12, 'D': 13,
    'E': 14, 'F': 15,
}


def generate():
    from PIL import Image

    w, h = 32, 48
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    pixels = img.load()

    for y, row in enumerate(RADIO):
        for x, ch in enumerate(row[:w]):
            idx = _CHAR_MAP.get(ch)
            if idx is not None:
                r, g, b = PYXEL_PALETTE[idx]
                pixels[x, y] = (r, g, b, 255)

    out = Path(__file__).resolve().parent.parent / "assets" / "radio_sprite.png"
    img.save(str(out))
    return out


if __name__ == "__main__":
    p = generate()
    print(f"Saved to {p}")
