"""Generate icon.ico (needs Pillow). Run once; the .ico is checked in next to this script."""
from pathlib import Path

from PIL import Image, ImageDraw

S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((40, 40, S - 40, S - 40), radius=210, fill="#d70028")
c, r = S // 2, 360
d.ellipse((c - r, c - r, c + r, c + r), fill="#000000")
for i, color in enumerate(("#262626", "#333333")):
    rr = r - 40 - i * 70
    d.ellipse((c - rr, c - rr, c + rr, c + rr), outline=color, width=26)
# Shine in the other PlayStation logo colors
for start, color in ((185, "#e6bb00"), (215, "#03a496"), (245, "#3068ac")):
    d.pieslice((c - r + 30, c - r + 30, c + r - 30, c + r - 30), start, start + 30, fill=color)
d.ellipse((c - 120, c - 120, c + 120, c + 120), fill="#d70028")
d.ellipse((c - 52, c - 52, c + 52, c + 52), fill="#000000")

img.save(Path(__file__).with_name("icon.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                                                     (128, 128), (256, 256)])
