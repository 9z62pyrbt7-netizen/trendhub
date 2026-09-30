# Test görselleri: beyaz fonlu (dekupe) ve renkli fonlu (fotoğraf benzeri) çanta çizimleri
from PIL import Image, ImageDraw, ImageFilter
import random, os
random.seed(7)
OUT = os.environ.get("E2E_IMG_DIR") or os.path.join(os.path.dirname(__file__), ".img")
os.makedirs(OUT, exist_ok=True)
COLORS = {"siyah": ((28,26,24),(45,40,36)), "taba": ((168,112,61),(120,78,40)), "bej": ((216,195,165),(180,150,112)),
          "bordo": ((94,26,34),(70,18,24)), "krem": ((237,227,209),(200,185,160)), "haki": ((111,106,72),(80,76,50)),
          "lacivert": ((31,42,68),(22,30,50)), "pudra": ((230,196,188),(196,160,150))}
def bag(body, trim, W=1200, H=1500, bg=(255,255,255), shape=0, angle=0, scale=1.0):
    im = Image.new("RGB", (W, H), bg)
    layer = Image.new("RGBA", (W, H), (0,0,0,0))
    d = ImageDraw.Draw(layer)
    cx, cy = W//2, int(H*0.56)
    s = scale
    w, h = int(760*s), int(520*s)
    if shape == 0:   # satchel
        d.polygon([(cx-w//2+40,cy-h//2),(cx+w//2-40,cy-h//2),(cx+w//2,cy+h//2),(cx-w//2,cy+h//2)], fill=body)
        d.arc([cx-200*s,cy-h//2-330*s,cx+200*s,cy-h//2+90*s], 180, 360, fill=trim, width=int(30*s))
    elif shape == 1: # omuz / hobo
        d.ellipse([cx-w//2,cy-h//2,cx+w//2,cy+h//2], fill=body)
        d.arc([cx-w//2+40,cy-h//2-420*s,cx+w//2-40,cy+60*s], 190, 350, fill=trim, width=int(22*s))
    elif shape == 2: # çapraz / kutu
        d.rounded_rectangle([cx-w//2+80,cy-h//2+60,cx+w//2-80,cy+h//2], radius=int(50*s), fill=body)
        d.line([(cx-w//2+90,cy-h//2+80),(cx-w//2-60,cy-h//2-520*s)], fill=trim, width=int(14*s))
        d.line([(cx+w//2-90,cy-h//2+80),(cx+w//2+60,cy-h//2-520*s)], fill=trim, width=int(14*s))
    else:            # tote
        d.polygon([(cx-w//2,cy-h//2),(cx+w//2,cy-h//2),(cx+w//2-40,cy+h//2),(cx-w//2+40,cy+h//2)], fill=body)
        d.arc([cx-280*s,cy-h//2-300*s,cx-40*s,cy-h//2+120*s], 180, 360, fill=trim, width=int(24*s))
        d.arc([cx+40*s,cy-h//2-300*s,cx+280*s,cy-h//2+120*s], 180, 360, fill=trim, width=int(24*s))
    d.rounded_rectangle([cx-60*s,cy-h//2+40*s,cx+60*s,cy-h//2+100*s], radius=10, fill=(201,164,92))
    layer = layer.rotate(angle, center=(cx,cy), resample=Image.BICUBIC)
    sh = Image.new("RGBA", (W,H), (0,0,0,0)); ImageDraw.Draw(sh).ellipse([cx-w//2,cy+h//2-10,cx+w//2,cy+h//2+50], fill=(0,0,0,60))
    im.paste(sh.filter(ImageFilter.GaussianBlur(18)), (0,0), sh.filter(ImageFilter.GaussianBlur(18)))
    im.paste(layer, (0,0), layer)
    return im
n = 0
for model in range(14):
    shape = model % 4
    colors = random.sample(list(COLORS), 2 if model % 3 == 0 else 1)
    lifestyle = model in (3, 8, 11)
    for c in colors:
        body, trim = COLORS[c]
        for k in range(4):
            bg = (255,255,255) if not lifestyle else [(214,196,176),(188,170,150),(232,220,205),(170,160,150)][k]
            im = bag(body, trim, bg=bg, shape=shape, angle=[0,-8,6,0][k], scale=[1,.9,1.35,.8][k])
            im.save(f"{OUT}/m{model}-{c}-{k}.jpg", quality=85)
            n += 1
print(n)
