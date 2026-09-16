"""Kruti Dev (legacy 8-bit Hindi DTP font) -> Unicode Devanagari.

Used by the CEO Uttarakhand *Polling Station List 2026* PDFs and the *Form 20
Vidhan Sabha 2012* PDFs. Text extracted from those PDFs looks like ASCII noise
(`jktdh; izkFkfed fo|ky;`) because the font maps Devanagari glyphs onto Latin
codepoints. The mapping is deterministic, so the conversion is lossless in
principle; in practice a few rare conjuncts are missing from the table, which is
why `convert()` also returns a confidence score.

Two ordering quirks make this more than a character substitution:

1. The short-i vowel sign (`ि`) is typed *before* its consonant in Kruti Dev but
   must follow it in Unicode, so it has to be moved one cluster to the right.
2. The reph (`र्`) is typed *after* the syllable it belongs to (as `Z`) and must
   be moved to the front of that cluster.
"""
from __future__ import annotations

import re

# Longest-match-first substitution table. Order matters: multi-character
# sequences must be tried before their prefixes.
_PAIRS: list[tuple[str, str]] = [
    # --- multi-character conjuncts and matras -------------------------
    ("ñ", "ॉ"), ("Q", "फ"), ("+", "़"),
    ("ºz", "फ्र"), ("ºM", "ॐ"),
    ("‘", "‘"), ("’", "’"), ("“", "“"), ("”", "”"),
    ("ª", "्र"), ("z", "्र"),
    ("Ñ", "क्र"), ("ø", "क्र"), ("Ø", "क्र"), ("æ", "क्र"),
    ("ó", "त्त्"), ("ù", "क्त"),
    ("Ç", "द्द"), ("ç", "प्र"), ("ý", "र्f"),
    ("¢", "ट्र"), ("£", "ठ्ठ"), ("¨", "ड्ड"),
    ("Ì", "ड्ढ"), ("Ë", "द्घ"), ("Í", "क्ष्"),
    ("Î", "श्र"), ("Ï", "श्च"), ("Ð", "ट्ठ"),
    ("Ñ", "क्र"), ("Ò", "द्भ"), ("Ó", "ड्ग"),
    ("Ô", "क्क"), ("Õ", "ह्न"), ("Ö", "ह्र"),
    ("×", "ह्म"), ("Ø", "क्र"), ("Ù", "ह्य"),
    ("Ú", "द्व"), ("Û", "ट्ट"), ("Ü", "श"),
    ("Ý", "ढ्ढ"), ("Þ", "झ्"), ("ß", "क्ष"),
    ("à", "ज्ञ"), ("á", "द्ध"), ("â", "ह्"),
    ("ã", "र्" ), ("ä", "ड़"), ("å", "ढ़"),
    ("ç", "प्र"), ("è", "द्र"), ("é", "प्र"),
    ("ê", "ट्र"), ("ë", "ड्र"), ("ì", "ढ्र"),
    ("í", "ह्न"), ("î", "ह्र"), ("ï", "ह्"),
    ("ð", "श्"), ("ñ", "ॉ"), ("ò", "ड्"),
    ("ô", "क्"), ("õ", "फ्"), ("ö", "क्र"),
    ("÷", "द्य"), ("ú", "झ"), ("û", "ट्"),
    ("ü", "ठ्"), ("þ", "श्"), ("ÿ", "ह्"),

    ("=", "त्र"), ("«", "त्र"),
    ("Kk", "ज्ञ"), ("K", "ज्ञ"),
    ("{k", "क्ष"), ("{", "क्ष्"),
    ("'k", "श"), ('"k', "ष"),
    ("J", "श्र"), ("N", "छ"),
    ("Vª", "ट्र"), ("Mª", "ड्र"), ("<ª", "ढ्र"),
    ("Nª", "छ्र"), ("Ø", "क्र"), ("æ", "क्र"),
    ("ÍkZ", "र्क्ष"),

    # --- vowels (independent) ------------------------------------------
    ("v‚", "ऑ"), ("vkS", "औ"), ("vks", "ओ"), ("vkW", "ऑ"),
    ("vk", "आ"), ("vks", "ओ"),
    ("v", "अ"),
    ("bZ", "ई"), ("b", "इ"),
    ("mQ", "ऊ"), ("Å", "ऊ"), ("m", "उ"),
    ("_", "ऋ"),
    (",s", "ऐ"), (",", "ए"),
    ("vkas", "ओं"),

    # --- consonants ----------------------------------------------------
    ("d", "क"), ("[k", "ख"), ("x", "ग"), ("?k", "घ"), ("³", "ङ"),
    ("p", "च"), ("N", "छ"), ("t", "ज"), (">", "झ"), ("¥", "ञ"),
    ("V", "ट"), ("B", "ठ"), ("M", "ड"), ("<", "ढ"), (".k", "ण"),
    ("r", "त"), ("Fk", "थ"), ("n", "द"), ("/k", "ध"), ("u", "न"),
    ("i", "प"), ("Q", "फ"), ("c", "ब"), ("Hk", "भ"), ("e", "म"),
    (";", "य"), ("j", "र"), ("y", "ल"), ("o", "व"),
    ("l", "स"), ("g", "ह"),
    ("G", "ॉ"), (":", "रू"), ("#", "रु"),

    # --- half consonants (virama forms) ---------------------------------
    ("D", "क्"), ("[", "ख्"), ("X", "ग्"), ("?", "घ्"),
    ("P", "च्"), ("T", "ज्"), ("©", "झ्"),
    (".", "ण्"), ("R", "त्"), ("F", "थ्"), ("/", "ध्"),
    ("U", "न्"), ("I", "प्"), ("C", "ब्"), ("H", "भ्"),
    ("E", "म्"), ("Y", "ल्"), ("O", "व्"),
    ("'", "श्"), ('"', "ष्"), ("L", "स्"),
    ("|", "द्य"), ("}", "द्व"), ("Ø", "क्र"),

    # --- matras ---------------------------------------------------------
    ("k", "ा"), ("f", "ि"), ("h", "ी"),
    ("q", "ु"), ("w", "ू"),
    ("`", "ृ"), ("`", "ृ"),
    ("s", "े"), ("S", "ै"),
    ("ks", "ो"), ("kS", "ौ"),
    ("a", "ं"), ("¡", "ँ"), ("%", "ः"),
    ("~", "्"), ("A", "।"),
    ("W", "ॅ"),

    # --- digits and punctuation ----------------------------------------
    ("&", "-"), ("¼", "("), ("½", ")"),
    ("μ", "०"),
]

# Build a longest-first regex so that e.g. "Fk" wins over "F".
_PAIRS_SORTED = sorted(_PAIRS, key=lambda p: -len(p[0]))
_LOOKUP = {}
for _k, _v in _PAIRS_SORTED:
    _LOOKUP.setdefault(_k, _v)
_PATTERN = re.compile("|".join(re.escape(k) for k in _LOOKUP))

# Consonants + their half forms, needed for the matra/reph reordering passes.
_CONSONANTS = set("कखगघङचछजझञटठडढणतथदधनपफबभमयरलवशषसह") | {"क़", "ख़", "ग़", "ज़", "ड़", "ढ़", "फ़"}
_VIRAMA = "्"
_SHORT_I = "ि"
_REPH = "र" + _VIRAMA


def _reorder_short_i(s: str) -> str:
    """Move `ि` from before its consonant cluster to after it."""
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        if s[i] == _SHORT_I:
            j = i + 1
            # Consume the following cluster: C (VIRAMA C)*
            if j < n and s[j] in _CONSONANTS:
                j += 1
                while j + 1 < n and s[j] == _VIRAMA and s[j + 1] in _CONSONANTS:
                    j += 2
                out.append(s[i + 1:j])
                out.append(_SHORT_I)
                i = j
                continue
        out.append(s[i])
        i += 1
    return "".join(out)


def _reorder_reph(s: str) -> str:
    """Kruti Dev writes reph after the cluster (`Z`); Unicode needs it before."""
    # In this table `Z` maps to the raw sequence "र्" placed after the syllable.
    # Move any "र्" that directly follows a consonant+matra group to the front
    # of that group.
    pattern = re.compile(
        r"([" + "".join(_CONSONANTS) + r"])"          # base consonant
        r"([ा-ौंःँ]*)"        # its matras / anusvara
        r"र्"                                 # reph typed after
    )
    # Single pass only: re.sub already handles every non-overlapping match, and
    # re-running it would move a reph that has just been placed correctly.
    return pattern.sub(lambda m: _REPH + m.group(1) + m.group(2), s)


_SUSPICIOUS = re.compile(r"[A-Za-z@#$^*\\]")


def convert(text: str) -> str:
    """Convert a Kruti Dev string to Unicode Devanagari."""
    if not text:
        return ""
    # 'Z' is the reph marker in Kruti Dev; handle it as a token so the reorder
    # pass can find it reliably.
    s = text.replace("Z", "र्")
    s = _PATTERN.sub(lambda m: _LOOKUP[m.group(0)], s)
    s = _reorder_short_i(s)
    s = _reorder_reph(s)
    return s


def convert_with_confidence(text: str) -> tuple[str, float]:
    """Convert, and score how much of the input was actually recognised.

    Confidence is 1.0 when no Latin letters survive the conversion, and falls
    toward 0 as unmapped characters remain.
    """
    if not text or not text.strip():
        return "", 0.0
    out = convert(text)
    leftovers = len(_SUSPICIOUS.findall(out))
    total = max(len(out.strip()), 1)
    return out, max(0.0, min(1.0, 1.0 - leftovers / total))


def looks_like_krutidev(text: str, threshold: float = 0.55) -> bool:
    """Heuristic: mostly-ASCII text with no Devanagari is probably Kruti Dev."""
    stripped = [c for c in text if not c.isspace()]
    if len(stripped) < 20:
        return False
    devanagari = sum(1 for c in stripped if "ऀ" <= c <= "ॿ")
    if devanagari / len(stripped) > 0.2:
        return False
    ascii_letters = sum(1 for c in stripped if c.isascii() and c.isalpha())
    return ascii_letters / len(stripped) >= threshold
