"""Generate hacker character sprite as pixel art for pyxel.

Creates a 64x120 pixel art image of a cyberpunk hacker:
- Dark hooded figure with mask/balaclava
- Tactical gear + backpack with cables
- Holding a green-screen device
- Dark palette matching pyxel's 16 colors

Output: assets/hacker_sprite.png (pyxel-ready, color 0 = transparent)
"""

from pathlib import Path


# Pyxel palette RGB values
PAL = {
    0:  (0x00, 0x00, 0x00),  # black (transparent)
    1:  (0x2B, 0x33, 0x5F),  # dark blue
    2:  (0x7E, 0x20, 0x72),  # purple
    3:  (0x19, 0x95, 0x9C),  # teal
    4:  (0x8B, 0x48, 0x52),  # brown
    5:  (0x39, 0x5C, 0x98),  # blue
    6:  (0xA9, 0xC1, 0xFF),  # light blue
    7:  (0xEE, 0xEE, 0xEE),  # white
    8:  (0xD4, 0x18, 0x6C),  # pink/red
    9:  (0xD3, 0x84, 0x41),  # orange
    10: (0xE9, 0xC3, 0x5B),  # yellow
    11: (0x70, 0xC6, 0xA9),  # green
    12: (0x76, 0x96, 0xDE),  # medium blue
    13: (0xA3, 0xA3, 0xA3),  # gray
    14: (0xFF, 0x97, 0x98),  # light pink
    15: (0xED, 0xB4, 0xA1),  # peach
}

_ = 0   # transparent
K = 0   # black (will be transparent in pyxel)
D = 1   # dark blue (darkest visible)
B = 5   # blue (dark clothing)
G = 13  # gray (highlights, straps)
W = 7   # white (bright details)
T = 3   # teal (device glow, accents)
N = 11  # green (device screen)
L = 6   # light blue (glow fx)
R = 4   # brown (leather straps)

# 64 wide x 120 tall sprite
# Each row is a list of palette indices
# This is a carefully designed pixel art of a tactical hacker

SPRITE_W = 64
SPRITE_H = 120


def _make_sprite_data():
    """Generate pixel data for hacker character."""
    # Start with transparent canvas (color 0 = black/transparent)
    data = [[0 for _x in range(SPRITE_W)] for _y in range(SPRITE_H)]

    def rect(x, y, w, h, c):
        for ry in range(y, min(y + h, SPRITE_H)):
            for rx in range(x, min(x + w, SPRITE_W)):
                if 0 <= rx < SPRITE_W and 0 <= ry < SPRITE_H:
                    data[ry][rx] = c

    def line_h(x, y, length, c):
        for rx in range(x, min(x + length, SPRITE_W)):
            if 0 <= rx < SPRITE_W and 0 <= y < SPRITE_H:
                data[y][rx] = c

    def line_v(x, y, length, c):
        for ry in range(y, min(y + length, SPRITE_H)):
            if 0 <= x < SPRITE_W and 0 <= ry < SPRITE_H:
                data[ry][x] = c

    def pset(x, y, c):
        if 0 <= x < SPRITE_W and 0 <= y < SPRITE_H:
            data[y][x] = c

    cx = 32  # center x

    # ─── HOOD (top) ──────────────────────────────
    # Hood dome
    rect(cx - 8, 2, 16, 4, D)
    rect(cx - 10, 6, 20, 3, D)
    rect(cx - 11, 9, 22, 16, D)
    # Hood rim (lighter edge)
    line_h(cx - 7, 3, 14, B)
    line_v(cx - 9, 9, 14, B)
    line_v(cx + 9, 9, 14, B)

    # ─── FACE (balaclava/mask, minimal skin) ─────
    rect(cx - 7, 10, 14, 14, 1)  # face shadow
    # Eyes only visible part
    pset(cx - 3, 15, W)  # left eye
    pset(cx + 3, 15, W)  # right eye
    pset(cx - 4, 14, G)  # left brow
    pset(cx - 3, 14, G)
    pset(cx + 3, 14, G)  # right brow
    pset(cx + 4, 14, G)

    # ─── NECK / COLLAR ──────────────────────────
    rect(cx - 6, 25, 12, 3, D)

    # ─── BACKPACK (behind character, visible edges) ──
    rect(cx + 9, 20, 8, 40, D)   # backpack body (right side)
    rect(cx + 10, 22, 6, 36, 1)  # backpack darker inside
    # Backpack straps
    line_v(cx + 9, 28, 20, G)
    line_v(cx + 15, 28, 20, G)
    # Cables from backpack
    pset(cx + 16, 30, G)
    pset(cx + 17, 31, G)
    pset(cx + 17, 33, G)
    pset(cx + 18, 34, G)
    pset(cx + 18, 36, G)
    pset(cx + 17, 38, G)
    pset(cx + 16, 40, G)
    pset(cx + 16, 42, G)
    pset(cx + 17, 44, G)

    # ─── TORSO (tactical jacket) ────────────────
    rect(cx - 12, 28, 24, 28, D)   # main body
    rect(cx - 10, 28, 20, 28, B)   # jacket
    rect(cx - 12, 28, 3, 28, D)    # left edge (shadow)
    rect(cx + 9,  28, 3, 28, D)    # right edge (shadow)
    # Center zipper
    line_v(cx, 30, 24, D)
    line_v(cx + 1, 30, 24, D)
    # Chest pockets
    rect(cx - 9, 34, 6, 4, D)
    rect(cx + 4, 34, 6, 4, D)
    line_h(cx - 9, 34, 6, G)  # pocket flap L
    line_h(cx + 4, 34, 6, G)  # pocket flap R
    # Shoulder straps / tactical vest
    line_v(cx - 8, 28, 12, G)
    line_v(cx + 8, 28, 12, G)

    # Belt
    rect(cx - 11, 54, 22, 2, G)
    rect(cx - 2, 53, 4, 4, W)  # buckle

    # ─── ARMS ────────────────────────────────────
    # Left arm (hanging, slight angle)
    rect(cx - 16, 30, 5, 22, D)
    rect(cx - 15, 30, 3, 22, B)
    # Left hand (gloved)
    rect(cx - 15, 52, 4, 5, D)

    # Right arm (bent, holding device forward)
    rect(cx + 12, 30, 5, 14, D)    # upper arm
    rect(cx + 13, 30, 3, 14, B)
    # Forearm (angled toward device)
    rect(cx + 10, 44, 5, 4, D)
    rect(cx + 7, 47, 5, 4, D)
    # Right hand (holding device)
    rect(cx + 4, 50, 5, 4, D)

    # ─── DEVICE (green screen glow) ─────────────
    dv_x = cx - 6
    dv_y = 46
    # Device body
    rect(dv_x, dv_y, 12, 8, D)
    rect(dv_x, dv_y, 12, 1, G)  # top edge
    rect(dv_x, dv_y + 7, 12, 1, G)  # bottom edge
    line_v(dv_x, dv_y, 8, G)  # left edge
    line_v(dv_x + 11, dv_y, 8, G)  # right edge
    # Screen (green glow!)
    rect(dv_x + 1, dv_y + 1, 10, 6, N)
    # Screen details (scan lines)
    line_h(dv_x + 1, dv_y + 2, 10, T)
    line_h(dv_x + 1, dv_y + 4, 10, T)
    # Screen data dots
    pset(dv_x + 3, dv_y + 3, W)
    pset(dv_x + 7, dv_y + 5, W)
    pset(dv_x + 5, dv_y + 2, L)
    # Glow effect around device
    pset(dv_x - 1, dv_y + 3, T)
    pset(dv_x + 12, dv_y + 3, T)
    pset(dv_x + 5, dv_y - 1, T)

    # ─── LEGS ────────────────────────────────────
    rect(cx - 9, 56, 8, 30, D)    # left leg
    rect(cx + 2, 56, 8, 30, D)    # right leg
    rect(cx - 8, 56, 6, 30, B)    # left inner
    rect(cx + 3, 56, 6, 30, B)    # right inner
    # Knee details
    line_h(cx - 8, 70, 6, D)
    line_h(cx + 3, 70, 6, D)
    # Cargo pocket patches
    rect(cx - 7, 74, 4, 3, D)
    rect(cx + 4, 74, 4, 3, D)

    # ─── BOOTS ───────────────────────────────────
    rect(cx - 10, 86, 9, 10, D)   # left boot
    rect(cx + 2, 86, 9, 10, D)    # right boot
    # Boot soles (darker)
    rect(cx - 10, 93, 9, 3, 1)
    rect(cx + 2, 93, 9, 3, 1)
    # Boot details (laces/straps)
    line_h(cx - 9, 87, 7, G)
    line_h(cx - 9, 89, 7, G)
    line_h(cx + 3, 87, 7, G)
    line_h(cx + 3, 89, 7, G)
    # Boot toe caps
    rect(cx - 11, 91, 3, 5, D)
    rect(cx + 9, 91, 3, 5, D)

    return data


def generate():
    """Generate and save the hacker sprite PNG."""
    from PIL import Image

    sprite_data = _make_sprite_data()
    img = Image.new("RGB", (SPRITE_W, SPRITE_H), (0, 0, 0))
    pixels = img.load()

    for y in range(SPRITE_H):
        for x in range(SPRITE_W):
            c = sprite_data[y][x]
            pixels[x, y] = PAL[c]

    out_path = Path(__file__).resolve().parent.parent / "assets" / "hacker_sprite.png"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(out_path))
    print(f"Saved: {out_path} ({SPRITE_W}x{SPRITE_H})")
    return str(out_path)


if __name__ == "__main__":
    generate()
