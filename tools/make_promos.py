"""Generate the built-in feature promo slides shown when the playlist is
empty. Outputs 1920x1080 PNGs to resources/media/. Run from the addon root:

    python tools/make_promos.py

Each slide shares the same design system (navy card, cyan accents, Segoe,
logo + wordmark, CTA pill) but uses a different layout, like template
variants in a slide deck.
"""

import os

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "resources", "media")
WWW = os.path.join(ROOT, "resources", "www")

NAVY = (10, 20, 40)
CARD = (16, 32, 60)
EDGE = (29, 58, 99)
CYAN = (62, 193, 243)
TEXT = (221, 230, 242)
DIM = (122, 155, 184)

W, H = 1920, 1080
INSET = 70  # outer card margin

FONT_DIR = os.environ.get("WINDIR", r"C:\Windows") + r"\Fonts"
BOLD = os.path.join(FONT_DIR, "segoeuib.ttf")
REG = os.path.join(FONT_DIR, "segoeui.ttf")

CTA = "This playlist is empty — add content in Kodi or via the web admin"

SLIDES = [
    dict(
        layout="center",
        title="Content that runs itself",
        body="Images, videos and live streams in one playlist —\n"
             "set the order, timing, shuffle and schedules, then walk away.",
    ),
    dict(
        layout="left",
        title="Always an intentional image",
        body="Never a blank screen, never a launcher.\n"
             "Every display presents something on purpose, all the time.",
    ),
    dict(
        layout="split",
        title="Manage from anywhere",
        body="A full admin page on your LAN: upload,\n"
             "reorder, schedule and preview — or pair\n"
             "displays to a Screensemble server.",
    ),
    dict(
        layout="right",
        title="Works completely offline",
        body="The built-in media folder keeps playing with no\n"
             "network at all. Add files from the admin page or\n"
             "right inside Kodi.",
    ),
]


def font(path, size):
    return ImageFont.truetype(path, size)


def rounded(draw, box, r, fill=None, outline=None, width=1):
    draw.rounded_rectangle(box, radius=r, fill=fill,
                           outline=outline, width=width)


def frame(img):
    """Navy backdrop + rounded inner card — common to every slide."""
    d = ImageDraw.Draw(img)
    rounded(d, (INSET, INSET, W - INSET, H - INSET), 48, fill=CARD,
            outline=EDGE, width=3)
    return d


def _brand_assets():
    logo = Image.open(os.path.join(WWW, "logo.png")).convert("RGBA")
    mark = Image.open(os.path.join(WWW, "wordmark.png")).convert("RGBA")
    logo.thumbnail((100, 100))
    mark.thumbnail((420, 84))
    return logo, mark, logo.width + 24 + mark.width


def brand(img, x, y):
    """Paste logo + wordmark with its left edge at x, vertically
    centered on y."""
    logo, mark, _ = _brand_assets()
    img.paste(logo, (x, y - logo.height // 2), logo)
    img.paste(mark, (x + logo.width + 24, y - mark.height // 2), mark)


def brand_centered(img, y):
    logo, mark, total = _brand_assets()
    x = (W - total) // 2
    img.paste(logo, (x, y - logo.height // 2), logo)
    img.paste(mark, (x + logo.width + 24, y - mark.height // 2), mark)


def brand_right(img, x_right, y):
    logo, mark, total = _brand_assets()
    brand(img, x_right - total, y)


def banner(d, pos):
    """CTA pill at pos: 'bc'|'br'|'bl'|'tc' — same message, moves
    per layout."""
    bf = font(BOLD, 34)
    bw = d.textlength(CTA, font=bf)
    pw, ph = bw + 88, 80
    if pos == "bc":
        x0, y0 = (W - pw) / 2, H - INSET - ph - 40
    elif pos == "br":
        x0, y0 = W - INSET - pw - 60, H - INSET - ph - 40
    elif pos == "bl":
        x0, y0 = INSET + 60, H - INSET - ph - 40
    elif pos == "tc":
        x0, y0 = (W - pw) / 2, INSET + 60
    else:
        raise ValueError(pos)
    rounded(d, (x0, y0, x0 + pw, y0 + ph), ph / 2, fill=CYAN)
    d.text((x0 + 44, y0 + (ph - 34) / 2 - 6), CTA, font=bf, fill=NAVY)


def center_text(d, y, text, fnt, fill):
    d.text(((W - d.textlength(text, font=fnt)) / 2, y),
           text, font=fnt, fill=fill)


def block(d, x, y, title, body, align):
    """Headline + body. align: 'left'|'right'|'center'."""
    hf, bf = font(BOLD, 88), font(REG, 42)

    def put(text, fnt, fill, yy):
        tw = d.textlength(text, font=fnt)
        if align == "left":
            tx = x
        elif align == "right":
            tx = x - tw
        else:
            tx = (W - tw) / 2
        d.text((tx, yy), text, font=fnt, fill=fill)

    put(title, hf, TEXT, y)
    y += 150
    for line in body.split("\n"):
        put(line, bf, DIM, y)
        y += 62


# --- layouts ------------------------------------------------------------

def layout_center(img, title, body):
    d = frame(img)
    brand_centered(img, 150)
    rounded(d, (W // 2 - 140, 268, W // 2 + 140, 276), 4, fill=CYAN)
    block(d, 0, 430, title, body, "center")
    banner(d, "bc")


def layout_left(img, title, body):
    d = frame(img)
    brand(img, INSET + 80, 150)
    # pull-quote accent bar beside the text block
    rounded(d, (INSET + 80, 420, INSET + 94, 780), 7, fill=CYAN)
    block(d, INSET + 140, 430, title, body, "left")
    # mini playlist motif, right side — three staggered rows,
    # the first highlighted as "now playing"
    rx = W - INSET - 560
    for i in range(3):
        ry = 580 + i * 105
        hl = i == 0
        rounded(d, (rx + i * 40, ry, rx + i * 40 + 420, ry + 84), 18,
                outline=CYAN if hl else EDGE,
                width=4 if hl else 3)
        d.ellipse((rx + i * 40 + 30, ry + 30, rx + i * 40 + 54,
                   ry + 54), fill=CYAN if hl else EDGE)
        rounded(d, (rx + i * 40 + 76, ry + 34, rx + i * 40 + 390,
                    ry + 50), 8, fill=EDGE)
    banner(d, "br")


def layout_split(img, title, body):
    d = frame(img)
    brand(img, INSET + 80, 150)
    # right-hand panel with concentric-ring motif
    px0, py0, px1, py1 = W - 780, 260, W - INSET - 80, H - INSET - 80
    rounded(d, (px0, py0, px1, py1), 36, outline=EDGE, width=3)
    cx, cy = (px0 + px1) // 2, (py0 + py1) // 2
    for r in (240, 170, 100):
        d.ellipse((cx - r, cy - r, cx + r, cy + r),
                  outline=CYAN if r == 100 else EDGE, width=4)
    d.ellipse((cx - 24, cy - 24, cx + 24, cy + 24), fill=CYAN)
    block(d, INSET + 80, 420, title, body, "left")
    banner(d, "bl")


def layout_right(img, title, body):
    d = frame(img)
    brand_right(img, W - INSET - 80, 150)
    # concentric rings anchored in the bottom-left corner
    cx, cy = INSET + 110, H - INSET - 40
    for r in (150, 230, 310):
        d.ellipse((cx - r, cy - r, cx + r, cy + r),
                  outline=CYAN if r == 150 else EDGE, width=4)
    # pull-quote accent bar beside the right-aligned text
    rounded(d, (W - INSET - 126, 420, W - INSET - 112, 780), 7,
            fill=CYAN)
    block(d, W - INSET - 140, 430, title, body, "right")
    banner(d, "bl")


LAYOUTS = {
    "center": layout_center,
    "left": layout_left,
    "split": layout_split,
    "right": layout_right,
}


def _dimmed(rgba, opacity):
    """Return a copy of an RGBA image with its alpha scaled."""
    img = rgba.copy()
    a = img.getchannel("A").point(lambda v: int(v * opacity))
    img.putalpha(a)
    return img


def make_standby():
    """Managed-mode empty state — a dimmed brand mark centered on pure
    black. Intentional standby, not self-promotion."""
    img = Image.new("RGB", (W, H), (0, 0, 0))
    logo, mark, _ = _brand_assets()
    # Slightly larger than the header treatment, gently dimmed.
    logo = logo.resize(
        (int(logo.width * 1.6), int(logo.height * 1.6)), Image.LANCZOS
    )
    mark = mark.resize((int(mark.width * 1.4), int(mark.height * 1.4)),
                       Image.LANCZOS)
    gap = 36
    total = logo.width + gap + mark.width
    x = (W - total) // 2
    y = H // 2
    dim_logo, dim_mark = _dimmed(logo, 0.38), _dimmed(mark, 0.30)
    img.paste(dim_logo, (x, y - logo.height // 2), dim_logo)
    img.paste(dim_mark, (x + logo.width + gap, y - mark.height // 2),
              dim_mark)
    out = os.path.join(OUT, "standby.png")
    img.convert("RGB").save(out)
    print("wrote", out)


def main():
    os.makedirs(OUT, exist_ok=True)
    for i, s in enumerate(SLIDES, 1):
        img = Image.new("RGB", (W, H), NAVY)
        LAYOUTS[s["layout"]](img, s["title"], s["body"])
        out = os.path.join(OUT, f"promo{i}.png")
        img.save(out)
        print("wrote", out)
    make_standby()


if __name__ == "__main__":
    main()
