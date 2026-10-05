"""Varlık-farkındalıklı para ayrıştırıcı (Türkçe).

Kurallar:
  * Tutar yalnızca para işaretli sayıdır: TL / ₺ / lira / liralık veya "bin" çarpanı. Çıplak sayı tutar DEĞİLDİR.
  * Ürün kodu / barkod / ürün adı ve harf+rakam karışık kodlar (PRE-1, SKU-X12, ABC123) metinden önce çıkarılır;
    içlerindeki rakamlar asla para sayılmaz.
  * Yüzdeler (%5, 5%) para değildir.
  * Biçimler: 7000 · 7.000 · 7 000 · 7.000,50 · 7,50 · 7,000 (virgülden sonra tam 3 hane → binlik ayırıcı) · 7 bin · 7,5 bin
  * Metinde birbirinden farklı birden fazla tutar varsa sonuç belirsizdir (amount=None, ambiguous=True); tahmin edilmez.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

_TR = str.maketrans("ıİçÇğĞöÖşŞüÜâ", "iiccggoossuua")
CODE = re.compile(r"(?<![\w])(?=[\w\-]*[a-z])(?=[\w\-]*\d)[a-z0-9]+(?:[\-_][a-z0-9]+)+(?![\w])|(?<![\w])(?=\w*[a-z])(?=\w*\d)[a-z0-9]{3,}(?![\w])")
PCT = re.compile(r"%\s*\d+(?:[.,]\d+)?|\d+(?:[.,]\d+)?\s*%")
NUM = r"\d{1,3}(?:[.\s]\d{3})+(?:,\d{1,2})?|\d+(?:[.,]\d+)?"
MONEY = re.compile(rf"(?<![\w.,])({NUM})\s*(bin\b)?\s*(tl\b|₺|liralik\b|liralık\b|lira\b)?", re.IGNORECASE)


@dataclass
class Money:
    amount: Decimal | None
    matches: list[str] = field(default_factory=list)
    ambiguous: bool = False


def fold(s: str) -> str:
    return (s or "").translate(_TR).lower()


def _number(tok: str, currency: bool) -> Decimal | None:
    t = tok.replace(" ", "")
    try:
        if re.fullmatch(r"\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?", t):            # 7.000 / 7.000,50
            return Decimal(t.replace(".", "").replace(",", "."))
        if re.fullmatch(r"\d{1,3}(?:,\d{3})+", t) and currency:              # 7,000 TL → binlik (bağlam: para birimi)
            return Decimal(t.replace(",", ""))
        if "," in t:                                                          # 7,50 → ondalık
            return Decimal(t.replace(",", "."))
        return Decimal(t)
    except InvalidOperation:
        return None


def parse_money(text: str, exclude: tuple | list = ()) -> Money:
    s = fold(text)
    for t in exclude:
        if t:
            s = s.replace(fold(str(t)), " ")
    s = re.sub(r"(\d)(tl|lira|liralik|liralık|bin)\b", r"\1 \2", s)     # 7000tl / 7bin → ayır (kod sanılmasın)
    s = PCT.sub(" ", s)
    s = CODE.sub(" ", s)
    found: list[tuple[Decimal, str]] = []
    for m in MONEY.finditer(s):
        num, thousand, cur = m.group(1), m.group(2), m.group(3)
        if not (thousand or cur):
            continue                                                          # çıplak sayı: para değil
        v = _number(num, True)
        if v is None:
            continue
        if thousand:
            v *= 1000
        if v > 0:
            found.append((v.quantize(Decimal("0.01")) if v != v.to_integral() else v.to_integral(), m.group(0).strip()))
    vals = {v for v, _ in found}
    if not vals:
        return Money(None)
    if len(vals) > 1:
        return Money(None, [m for _, m in found], ambiguous=True)
    return Money(next(iter(vals)), [m for _, m in found])
