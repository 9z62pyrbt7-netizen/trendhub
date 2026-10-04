# YALNIZCA YEREL TEST: stüdyo çekimine benzeyen çanta çizimleri (GERÇEK ÜRÜN DEĞİLDİR).
# Çoğu görsel tedarikçi fotoğrafları gibi beyaz fonludur (dekupe); bazı modeller renkli fonlu (lifestyle benzeri).
# Dosya adları seed_dev.py ile uyumludur: m{model}-{renk}-{0..3}.jpg
from PIL import Image, ImageChops, ImageDraw, ImageFilter
import os
import random

random.seed(7)
OUT = os.environ.get("E2E_IMG_DIR") or os.path.join(os.path.dirname(__file__), ".img")
os.makedirs(OUT, exist_ok=True)
SS = 2  # süper örnekleme (kenar yumuşatma)
W, H = 1200, 1500
COLORS = {"siyah": (34, 31, 29), "taba": (160, 101, 55), "bej": (214, 192, 160), "bordo": (98, 30, 38),
          "krem": (236, 226, 207), "haki": (112, 106, 74), "lacivert": (36, 46, 72), "pudra": (226, 190, 180)}
GOLD = ((236, 205, 140), (176, 132, 62))


def grad(size, c1, c2, horizontal=False):
    """İki renk arasında doğrusal geçiş (PIL ile)."""
    g = Image.linear_gradient("L").resize(size)
    if horizontal:
        g = g.rotate(90, expand=False).resize(size)
    return Image.composite(Image.new("RGB", size, c2), Image.new("RGB", size, c1), g)


def shade(base, mask, box, light=1.18, dark=0.62):
    """Gövdeyi hacimli gösterir: üstten-soldan ışık, alt/kenarlarda gölge."""
    x0, y0, x1, y1 = [int(v) for v in box]
    w, h = max(1, x1 - x0), max(1, y1 - y0)
    clamp = lambda v: tuple(max(0, min(255, int(c * v))) for c in base)
    vert = grad((w, h), clamp(light), clamp(dark))
    side = Image.radial_gradient("L").resize((int(w * 1.5), int(h * 1.7))).crop((int(w * .15), int(h * .25), int(w * 1.15), int(h * 1.25)))
    side = side.point(lambda p: 255 - int(p * .55))
    vert = ImageChops.multiply(vert, Image.merge("RGB", (side, side, side)))
    layer = Image.new("RGB", mask.size, base)
    layer.paste(vert, (x0, y0))
    return layer


def draw_bag(color, style, bg=(255, 255, 255), angle=0.0, scale=1.0, dy=0.0, view=0):
    S = SS
    im = Image.new("RGB", (W * S, H * S), bg)
    cx, cy = W * S // 2, int(H * S * (0.58 + dy))
    w, h = int(700 * scale * S), int(500 * scale * S)
    body_mask = Image.new("L", im.size, 0)
    d = ImageDraw.Draw(body_mask)
    if style == 0:      # satchel / el çantası (yamuk)
        box = (cx - w // 2, cy - h // 2, cx + w // 2, cy + h // 2)
        d.rounded_rectangle(box, radius=int(46 * S * scale), fill=255)
        d.polygon([(box[0], box[1] + 60 * S), (box[0] + 60 * S, box[1]), (box[0] + 60 * S, box[3])], fill=255)
    elif style == 1:    # hobo / omuz (yarım ay)
        box = (cx - w // 2, cy - int(h * .5), cx + w // 2, cy + h // 2)
        d.chord((box[0], box[1] - (box[3] - box[1]), box[2], box[3]), 0, 180, fill=255)
    elif style == 2:    # kutu / çapraz
        w, h = int(w * .78), int(h * .82)
        box = (cx - w // 2, cy - h // 2, cx + w // 2, cy + h // 2)
        d.rounded_rectangle(box, radius=int(70 * S * scale), fill=255)
    else:               # tote (alta doğru daralan)
        box = (cx - w // 2, cy - int(h * .55), cx + w // 2, cy + h // 2)
        d.polygon([(box[0], box[1]), (box[2], box[1]), (box[2] - 50 * S, box[3]), (box[0] + 50 * S, box[3])], fill=255)
    # gölgeler
    shadow = Image.new("L", im.size, 0)
    sd = ImageDraw.Draw(shadow)
    sd.ellipse((box[0] + 20 * S, box[3] - 18 * S, box[2] - 20 * S, box[3] + 34 * S), fill=120)
    shadow = shadow.filter(ImageFilter.GaussianBlur(26 * S))
    im = Image.composite(Image.new("RGB", im.size, tuple(int(c * .72) for c in bg)), im, shadow)
    soft = body_mask.filter(ImageFilter.GaussianBlur(60 * S)).point(lambda p: int(p * .28))
    soft = ImageChops.offset(soft, 0, 40 * S)
    im = Image.composite(Image.new("RGB", im.size, tuple(int(c * .8) for c in bg)), im, soft)
    # sap / askı (gövdenin arkasında)
    hd = ImageDraw.Draw(im)
    dark = tuple(int(c * .7) for c in color)
    if style in (0, 3):
        hw = int(w * (.28 if style == 0 else .2))
        for off in ((0,) if style == 0 else (-int(w * .2), int(w * .2))):
            hb = (cx + off - hw, box[1] - int(h * .62), cx + off + hw, box[1] + int(h * .25))
            hd.arc(hb, 180, 360, fill=dark, width=int(30 * S * scale))
            hd.arc((hb[0] + 8 * S, hb[1] + 8 * S, hb[2] - 8 * S, hb[3]), 196, 344, fill=tuple(min(255, int(c * 1.15)) for c in color), width=int(5 * S * scale))
    elif style == 1:
        hb = (box[0] + 50 * S, box[1] - int(h * .95), box[2] - 50 * S, box[1] + int(h * .5))
        hd.arc(hb, 186, 354, fill=dark, width=int(26 * S * scale))
    else:
        for i in range(0, 46):
            t = i / 45
            x = box[0] + 30 * S + (box[2] - box[0] - 60 * S) * t
            y = box[1] + 10 * S - (1 - (2 * t - 1) ** 2) * 560 * S * scale
            r = 7 * S * scale
            hd.ellipse((x - r, y - r, x + r, y + r), outline=GOLD[1], width=int(3 * S), fill=GOLD[0] if i % 2 else None)
    # gövde
    body = shade(color, body_mask, box)
    im = Image.composite(body, im, body_mask)
    bd = ImageDraw.Draw(im)
    # kapak / üst bant
    if style in (0, 2):
        flap_h = int((box[3] - box[1]) * (.46 if style == 0 else .52))
        flap = Image.new("L", im.size, 0)
        fd = ImageDraw.Draw(flap)
        fd.rounded_rectangle((box[0] + 6 * S, box[1], box[2] - 6 * S, box[1] + flap_h), radius=int(46 * S * scale), fill=255)
        fd.rectangle((box[0] + 6 * S, box[1], box[2] - 6 * S, box[1] + flap_h // 2), fill=255)
        flap = ImageChops.multiply(flap, body_mask)
        fl = shade(tuple(int(c * .9) for c in color), flap, (box[0], box[1], box[2], box[1] + flap_h), 1.25, .8)
        edge = flap.filter(ImageFilter.GaussianBlur(6 * S))
        im = Image.composite(Image.new("RGB", im.size, tuple(int(c * .55) for c in color)), im, ImageChops.offset(edge, 0, 8 * S).point(lambda p: int(p * .5)))
        im = Image.composite(fl, im, flap)
        bd = ImageDraw.Draw(im)
        stitch = tuple(min(255, int(c * 1.35) + 18) for c in color)
        y = box[1] + flap_h - 22 * S
        for x in range(int(box[0] + 50 * S), int(box[2] - 50 * S), int(26 * S)):
            bd.line((x, y, x + 13 * S, y), fill=stitch, width=int(3 * S))
        # metal kilit
        lx, ly = cx, box[1] + flap_h - 6 * S
        lb = (lx - 46 * S * scale, ly - 30 * S * scale, lx + 46 * S * scale, ly + 30 * S * scale)
        g = grad((int(lb[2] - lb[0]), int(lb[3] - lb[1])), GOLD[0], GOLD[1])
        m = Image.new("L", g.size, 0)
        ImageDraw.Draw(m).rounded_rectangle((0, 0, g.size[0] - 1, g.size[1] - 1), radius=int(12 * S), fill=255)
        im.paste(g, (int(lb[0]), int(lb[1])), m)
        bd = ImageDraw.Draw(im)
        bd.rounded_rectangle((lb[0] + 14 * S, lb[1] + 12 * S, lb[2] - 14 * S, lb[3] - 12 * S), radius=int(8 * S), outline=GOLD[1], width=int(3 * S))
    else:
        stitch = tuple(min(255, int(c * 1.3) + 16) for c in color)
        y = box[1] + 44 * S
        for x in range(int(box[0] + 70 * S), int(box[2] - 70 * S), int(26 * S)):
            bd.line((x, y, x + 13 * S, y), fill=stitch, width=int(3 * S))
        bd.rounded_rectangle((cx - 34 * S, box[1] + 70 * S, cx + 34 * S, box[1] + 100 * S), radius=int(10 * S), fill=GOLD[0], outline=GOLD[1], width=int(3 * S))
    if view == 2:  # yakın çekim detay
        cw = int(W * S * .72)
        ch = int(cw * 1.25)
        top = max(0, int(box[1] - ch * .3))
        im = im.crop((cx - cw // 2, top, cx + cw // 2, top + ch)).resize((W * S, H * S), Image.LANCZOS)
    if angle:
        im = im.rotate(angle, resample=Image.BICUBIC, fillcolor=bg)
    return im.resize((W, H), Image.LANCZOS)


n = 0
for model in range(14):
    style = model % 4
    colors = random.sample(list(COLORS), 2 if model % 3 == 0 else 1)
    lifestyle = model in (3, 8, 11)
    for c in colors:
        for k in range(4):
            bg = (255, 255, 255) if not lifestyle else [(226, 214, 198), (205, 190, 172), (236, 228, 216), (190, 178, 166)][k]
            im = draw_bag(COLORS[c], style, bg=bg, angle=[0, -5, 0, 4][k], scale=[1.08, .98, 1.0, .9][k], dy=[0, .01, 0, .02][k], view=k)
            im.save(f"{OUT}/m{model}-{c}-{k}.jpg", quality=86)
            n += 1
print(n)
