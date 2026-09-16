"""Repair Devanagari conjuncts mangled into Latin-Extended codepoints.

The CEO Uttarakhand *Legacy Roll 2003* PDFs embed a subsetted Hindi font whose
`ToUnicode` CMap maps conjunct glyphs onto Latin Extended-B / IPA codepoints
instead of onto Devanagari sequences. Extraction therefore yields *almost* correct
Unicode:

    "पुŜष"      -> should be "पुरुष"
    "संƥा"      -> should be "संख्या"
    "िनवाŊचक"   -> should be "निर्वाचक"

Base letters, digits, ASCII (EPIC numbers) and most matras come through
correctly, so the numeric and EPIC fields are unaffected. Only the conjuncts
need repair, and a leading short-i still has to be reordered.

The table below was derived by reading real Uttarakhand roll pages and matching
each stray codepoint against the word it must spell. Anything still unmapped is
counted against the confidence score rather than silently guessed at.
"""
from __future__ import annotations

import re

# Stray codepoint -> correct Devanagari sequence.
GLYPH_MAP: dict[str, str] = {
    # nasal / stop conjuncts
    "ȅ": "त्त",
    "ȶ": "न्त",
    "Ƚ": "न्द",
    "Ⱦ": "न्द्र",
    "ɀ": "न्ध",
    "ɾ": "म्ब",
    "Ȉ": "त्थ",
    "ɬ": "द्द",
    "ǳ": "द्द",
    "Ǜ": "ज्य",
    "Ǎ": "ज्ज",
    "ǵ": "ज्ज्व",
    "ȸ": "न्न",
    "Ʉ": "न्न",
    "Ƶ": "ख्य",
    "Ȫ": "त्न",
    # sibilant / semivowel conjuncts
    "ˢ": "स्व",
    "˕": "स्थ",
    "ˋ": "स्क",
    "ː": "स्ट",
    "˘": "स्न",
    "ˇ": "स्त",
    "ʴ": "श्य",
    "ʳ": "श्म",
    "ʷ": "श्व",
    "ʱ": "श्न",
    "ʃ": "म्म",
    "ʈ": "म्ह",
    "ʟ": "ल्ल",
    "ʙ": "ल्प",
    "ʮ": "ह्य",
    "ʵ": "ह्व",
    # k / kh / g family
    "ƥ": "ख्य",
    "Ɨ": "क्ष",
    "Ƙ": "क्ष्म",
    "ƚ": "क्त",
    "Ţ": "क्र",
    "Ţ": "क्र",
    "Ǣ": "ग्य",
    "Ȥ": "ग्र",
    # p / pr family
    "Ů": "प्र",
    "ů": "प्त",
    "Ű": "प्प",
    # retroflex
    "ˁ": "ष्ण",
    "ʼn": "ष्ट",
    "Ŕ": "ष्ठ",
    "ǅ": "ड्ड",
    "Ǆ": "ट्ट",
    "ǆ": "ड्ग",
    # r-based
    "Ŝ": "रु",
    "ŝ": "रू",
    "Ŋ": "र्",          # reph, typed AFTER its cluster
    "ŗ": "र्य",
    "į": "र",           # ri-matra carrier seen in "सįरता" = सरिता
    "Ō": "्र",          # ra-kaar
    "į": "ि",         # a bare short-i that still needs reordering:
                        # "सįरता" is सरिता (sarita), not सररिता
    "Ř": "ृ",
    # nasalised vowels / matra artefacts
    "Ő": "ें",
    "ő": "ैं",
    "Ŏ": "ों",
    "İ": "",            # zero-width carrier before a conjunct
    "ʎ": "ल्य",
    "ʏ": "ण्य",
    "ǐ": "द्ध",
    "ǒ": "द्व",
    "ǔ": "द्भ",
    "ȡ": "त्र",
    "ũ": "त्र",
    "ɡ": "ग",
    "Ɋ": "ञ",
    "ȷ": "ज्ञ",
    "ʙ": "ल्प",
    "ʝ": "ह्न",
    "Ȱ": "न्ह",
    "ǹ": "ङ",
    "ʁ": "ह्म",
    "Ⱥ": "त्स",
    "ʆ": "म्प",
    "ˀ": "स्प",
    "ˆ": "स्म",
    "ˍ": "स्य",
    "ʘ": "ल्ह",
    "Ɂ": "अ",
    "ɕ": "श",
    "ʺ": "ह्ल",
    "ɥ": "ह्",
    "ǁ": "द्म",
    "Ȍ": "त्म",
    "Ȏ": "त्व",
    "Ȓ": "त्य",
    "ȑ": "त्र्य",
    "ɑ": "आ",
    "Ɇ": "ए",
    # --- confirmed by auditing every low-confidence row across three real
    # --- parts and reading the surrounding word. Glyphs whose intended
    # --- conjunct remains genuinely ambiguous are left OUT on purpose: they
    # --- keep lowering the confidence score instead of being guessed at.
    "ŵ": "श्र",          # shri / shraddha
    "˽": "क्ष्",   # lakshmi
    "ˑ": "स्त",          # mastram
    "ȵ": "न्न",          # jhannu
    "˃": "ष्प",          # pushpa
    "Ɏ": "न्ह",          # kanhai
    "ț": "थ्व",          # prithvi
    "˝": "स्म",          # ismail
    "ȯ": "ध्य",          # dhyan
    "ɼ": "म्म",          # sammati
    "Š": "हु",                 # hukam
    "ʔ": "ल्त",          # sultan
    "ş": "हृ",                 # hridayram
    "Ÿ": "हृ",                 # hriday
    "ȏ": "त्म",          # atmaram
    "ɗ": "प्य",          # pyare
    "ƛ": "क्श",          # bakshi
    "Ƿ": "ण्ड",          # khanduri
    "ƨ": "ग्ग",          # jaggu
    "ſ": "ग्र",          # degree
}

_SHORT_I = "ि"
_VIRAMA = "्"
_REPH = "र" + _VIRAMA
_CONSONANTS = set("कखगघङचछजझञटठडढणतथदधनपफबभमयरलवशषसह")
_MATRAS = "ा-ौँ-ः़"

# Codepoints that should never survive a successful repair: Latin Extended-A/B,
# IPA extensions, spacing modifiers, and Greek — all signs of an unmapped glyph.
# The embedded font emits stray Latin combining marks that are not part of
# the Devanagari text (e.g. "62̻", "पित̻").
_STRAY_COMBINING = re.compile(r"[̀-ͯ]")
_SUSPICIOUS = re.compile(r"[Ā-˿Ͱ-Ͽ]")

# Longest-first alternation rather than str.maketrans: a few stray glyphs
# decompose into more than one codepoint, which maketrans rejects.
_GLYPH_RE = re.compile(
    "|".join(re.escape(k) for k in sorted(GLYPH_MAP, key=len, reverse=True))
)


def _reorder_short_i(s: str) -> str:
    """Move a leading `ि` to after its consonant cluster."""
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        if s[i] == _SHORT_I and i + 1 < n and s[i + 1] in _CONSONANTS:
            j = i + 1
            while j + 2 < n and s[j + 1] == _VIRAMA and s[j + 2] in _CONSONANTS:
                j += 2
            out.append(s[i + 1:j + 1])
            out.append(_SHORT_I)
            i = j + 1
            continue
        out.append(s[i])
        i += 1
    return "".join(out)


_REPH_RE = re.compile(rf"([{''.join(_CONSONANTS)}])([{_MATRAS}]*)र्")


def _reorder_reph(s: str) -> str:
    """`Ŋ` decodes to `र्` placed after its cluster; Unicode wants it before."""
    return _REPH_RE.sub(lambda m: _REPH + m.group(1) + m.group(2), s)


def clean(text: str) -> str:
    """Repair mangled conjuncts and normalise whitespace / NULL literals."""
    if not text:
        return ""
    s = _GLYPH_RE.sub(lambda m: GLYPH_MAP[m.group(0)], text)
    s = _reorder_short_i(s)
    s = _reorder_reph(s)
    s = _STRAY_COMBINING.sub("", s)
    # The source database exports literal "NULL" into empty text fields.
    s = re.sub(r"\bNULL\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def confidence(cleaned: str) -> float:
    """Share of the cleaned string that is not an unmapped stray glyph."""
    if not cleaned:
        return 0.0
    strays = len(_SUSPICIOUS.findall(cleaned))
    return max(0.0, min(1.0, 1.0 - strays / max(len(cleaned), 1)))


def clean_with_confidence(text: str) -> tuple[str, float]:
    out = clean(text)
    return out, confidence(out)
