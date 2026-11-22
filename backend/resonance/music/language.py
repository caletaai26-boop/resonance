"""Rule-based Latin/Spanish text helpers: syllabification, a singer's pronunciation guide for
ecclesiastical (Roman) Latin, and public-domain reference texts used ONLY for comparison."""
from __future__ import annotations

import re
import unicodedata

VOWELS = "aeiouyáéíóúüæœ"
# clusters that stay together at the start of a syllable
ONSETS = {"bl", "br", "cl", "cr", "dr", "fl", "fr", "gl", "gr", "pl", "pr", "tr", "ch", "ph", "th", "qu", "gu", "ll", "rr", "gn"}
DIPHTHONGS = {"la": {"ae", "oe", "au"}, "es": {"ai", "ei", "oi", "au", "eu", "ia", "ie", "io", "ua", "ue", "uo", "iu", "ui", "ay", "ey", "oy"},
              "en": set()}


def syllabify(word: str, lang: str = "la") -> list[str]:
    """Maximal-onset syllabifier. Good enough for underlay drafts; every result is flagged for review."""
    w = word
    low = w.lower()
    letters = [c for c in low]
    if not any(c in VOWELS for c in letters):
        return [w]
    dip = DIPHTHONGS.get(lang, set())
    # find vowel nuclei as (start, end)
    nuclei, i = [], 0
    while i < len(low):
        if low[i] in VOWELS:
            # 'u' after q/g(+vowel) is part of the onset, not a nucleus
            if low[i] == "u" and i > 0 and low[i - 1] in "qg" and i + 1 < len(low) and low[i + 1] in VOWELS and (low[i - 1] == "q" or lang == "es"):
                i += 1
                continue
            j = i + 1
            if j < len(low) and low[i:j + 1] in dip:
                j += 1
            nuclei.append((i, j))
            i = j
        else:
            i += 1
    if len(nuclei) <= 1:
        return [w]
    cuts = []
    for (a0, a1), (b0, _) in zip(nuclei[:-1], nuclei[1:]):
        cons = low[a1:b0]
        if len(cons) == 0:
            cuts.append(a1)
        elif len(cons) == 1:
            cuts.append(a1)
        else:
            k = len(cons) - 1
            if cons[-2:] in ONSETS:
                k = len(cons) - 2
            cuts.append(a1 + k)
    out, prev = [], 0
    for c in cuts:
        out.append(w[prev:c]); prev = c
    out.append(w[prev:])
    return [s for s in out if s]


def tokenize(line: str) -> list[str]:
    return re.findall(r"[A-Za-zÀ-ÿæœÆŒ]+", line)


def line_syllables(line: str, lang: str = "la") -> list[str]:
    """Syllables with hyphens marking word-internal continuation, as used for lyric underlay."""
    out = []
    for w in tokenize(line):
        syl = syllabify(w, lang)
        out += [s + "-" if k < len(syl) - 1 else s for k, s in enumerate(syl)]
    return out


def _strip(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def latin_pronunciation(word: str) -> str:
    """Ecclesiastical (Italianate) Latin respelling for English-speaking singers.
    c/g soften before e, i, ae, oe; 'ti'+vowel = 'tsee'; 'gn' = 'ny'; h is silent except 'mihi/nihil'."""
    w = _strip(word.lower()).replace("æ", "ae").replace("œ", "oe")
    out, i = [], 0
    front = ("e", "i", "y")

    def nxt(k: int) -> str:
        return w[k] if k < len(w) else ""

    def soft(k: int) -> bool:
        return nxt(k) in front or w[k:k + 2] in ("ae", "oe")

    while i < len(w):
        two, c = w[i:i + 2], w[i]
        if two in ("ae", "oe"):
            out.append("eh"); i += 2
        elif two == "au":
            out.append("ow"); i += 2
        elif two == "ch":
            out.append("k"); i += 2
        elif two == "ph":
            out.append("f"); i += 2
        elif two == "th":
            out.append("t"); i += 2
        elif two == "gn":
            out.append("ny"); i += 2
        elif two == "qu":
            out.append("kw"); i += 2
        elif two == "sc" and soft(i + 2):
            out.append("sh"); i += 2
        elif two == "cc" and soft(i + 2):
            out.append("t-ch"); i += 2
        elif two == "xc" and soft(i + 2):
            out.append("k-sh"); i += 2
        elif c == "c":
            out.append("ch" if soft(i + 1) else "k"); i += 1
        elif c == "g":
            out.append("j" if soft(i + 1) else "g"); i += 1
        elif c == "t" and nxt(i + 1) == "i" and nxt(i + 2) in "aeiou" and nxt(i + 2) and (i == 0 or w[i - 1] not in "stx"):
            out.append("ts"); i += 1
        elif c == "j":
            out.append("y"); i += 1
        elif c == "h":
            i += 1
        elif c == "v":
            out.append("v"); i += 1
        elif c == "x":
            out.append("ks"); i += 1
        elif c == "a":
            out.append("ah"); i += 1
        elif c == "e":
            out.append("eh"); i += 1
        elif c in "iy":
            out.append("ee"); i += 1
        elif c == "o":
            out.append("oh"); i += 1
        elif c == "u":
            out.append("oo"); i += 1
        else:
            out.append(c); i += 1
    return "·".join(out) if len(out) < 2 else "".join(
        (p if k == 0 else ("-" + p if p in ("ah", "eh", "ee", "oh", "oo", "ow") and out[k - 1] in ("ah", "eh", "ee", "oh", "oo", "ow") else p))
        for k, p in enumerate(out))


def pronunciation_guide(lines: list[str], lang: str) -> list[dict]:
    seen, out = set(), []
    for line in lines:
        for w in tokenize(line):
            k = w.lower()
            if k in seen:
                continue
            seen.add(k)
            entry = {"word": w, "syllables": syllabify(w, lang)}
            if lang == "la":
                entry["guide"] = latin_pronunciation(w)
            out.append(entry)
    return out


# Public-domain reference texts. They are offered for comparison only and are never treated as what was sung.
REFERENCE_TEXTS = {
    "adeste_fideles_la": {
        "title": "Adeste Fideles (Latin; text attributed to J. F. Wade, c. 1743, with later verses)",
        "language": "la",
        "rights": "Public domain",
        "note": "Published reference text. Choirs sing different selections and orders of verses; some repeat lines. Check every line against your recording.",
        "blocks": [
            {"kind": "verse", "label": "Verse 'Adeste fideles'", "lines": ["Adeste fideles, laeti triumphantes,", "Venite, venite in Bethlehem.", "Natum videte Regem angelorum."]},
            {"kind": "refrain", "label": "Refrain", "lines": ["Venite adoremus,", "Venite adoremus,", "Venite adoremus Dominum."]},
            {"kind": "verse", "label": "Verse 'Deum de Deo'", "lines": ["Deum de Deo, lumen de lumine,", "Gestant puellae viscera.", "Deum verum, genitum non factum."]},
            {"kind": "verse", "label": "Verse 'Cantet nunc io'", "lines": ["Cantet nunc io chorus angelorum;", "Cantet nunc aula caelestium,", "Gloria, gloria in excelsis Deo."]},
            {"kind": "verse", "label": "Verse 'Ergo qui natus'", "lines": ["Ergo qui natus die hodierna,", "Jesu, tibi sit gloria,", "Patris aeterni Verbum caro factum."]},
        ],
    },
}
