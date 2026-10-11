"""ホーム画面用のアイコン（PNG）を作る。ファビコンと同じ魚を Pillow で描く。

    python tools/make_icons.py

static/ に icon-192.png・icon-512.png（角丸）、icon-maskable-512.png（全面塗り・Android の切り抜き用）、
apple-touch-icon.png（180px）を書き出す。
"""
import os

from PIL import Image, ImageDraw

BG = (10, 14, 13)
FISH = (95, 224, 138)
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")


def cubic(p0, p1, p2, p3, n=24):
    return [tuple((1 - t) ** 3 * a + 3 * (1 - t) ** 2 * t * b + 3 * (1 - t) * t ** 2 * c + t ** 3 * d
                  for a, b, c, d in zip(p0, p1, p2, p3)) for t in (i / n for i in range(1, n + 1))]


# ファビコンの SVG パス（64 単位）と同じ形：鼻先 → 背中 → 尾びれ → 腹
FISH_PATH = ([(7, 32)] + cubic((7, 32), (14, 21), (27, 17), (40, 22))
             + [(54, 13), (54, 51), (40, 42)] + cubic((40, 42), (27, 47), (14, 43), (7, 32)))
EYE = (18, 29, 3.2)


def draw(size, maskable):
    ss = 4  # 縁をなめらかにするため 4 倍で描いて縮める
    big = size * ss
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if maskable:
        d.rectangle([0, 0, big, big], fill=BG)
        scale, off = big / 64 * 0.72, big * 0.14  # 中央 72% に収める（切り抜かれても欠けない）
    else:
        d.rounded_rectangle([0, 0, big - 1, big - 1], radius=big * 14 / 64, fill=BG)
        scale, off = big / 64, 0
    pts = [(off + x * scale, off + y * scale) for x, y in FISH_PATH]
    d.polygon(pts, fill=FISH)
    ex, ey, er = EYE
    d.ellipse([off + (ex - er) * scale, off + (ey - er) * scale, off + (ex + er) * scale, off + (ey + er) * scale], fill=BG)
    return img.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    for name, size, maskable in [("icon-192.png", 192, False), ("icon-512.png", 512, False),
                                 ("icon-maskable-512.png", 512, True), ("apple-touch-icon.png", 180, True)]:
        draw(size, maskable).save(os.path.join(OUT, name), optimize=True)
        print("wrote", name)
