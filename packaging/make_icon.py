"""Generate packaging/app.ico (and a PNG for the installer wizard) with Pillow."""
from pathlib import Path
from PIL import Image, ImageDraw

HERE = Path(__file__).parent
S = 1024  # draw large, then downscale for smooth edges
ACCENT = (124, 140, 255, 255)
INK = (11, 13, 26, 255)


def draw(size: int) -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = 40
    d.rounded_rectangle([pad, pad, S - pad, S - pad], radius=230, fill=ACCENT)
    w = 92
    cx = S // 2
    top, tip = 250, 640
    d.line([(cx, top), (cx, tip)], fill=INK, width=w)
    d.line([(cx - 190, tip - 190), (cx, tip)], fill=INK, width=w, joint="curve")
    d.line([(cx + 190, tip - 190), (cx, tip)], fill=INK, width=w, joint="curve")
    for x, y in [(cx, top), (cx - 190, tip - 190), (cx + 190, tip - 190), (cx, tip)]:
        d.ellipse([x - w / 2, y - w / 2, x + w / 2, y + w / 2], fill=INK)
    by = 790
    d.line([(cx - 250, by), (cx + 250, by)], fill=INK, width=w)
    for x in (cx - 250, cx + 250):
        d.ellipse([x - w / 2, by - w / 2, x + w / 2, by + w / 2], fill=INK)
    return img.resize((size, size), Image.LANCZOS)


big = draw(256)
big.save(HERE / "app.ico", sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40),
                                  (48, 48), (64, 64), (128, 128), (256, 256)])
draw(256).save(HERE / "app.png")

# Small image shown in the top-right of the Inno Setup wizard (BMP, no alpha).
for px, name in [(55, "wizard-small.bmp"), (110, "wizard-small-2x.bmp")]:
    bg = Image.new("RGB", (px, px), (255, 255, 255))
    ic = draw(px)
    bg.paste(ic, (0, 0), ic)
    bg.save(HERE / name)
print("icons written")
