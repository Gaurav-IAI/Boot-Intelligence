"""Party identity on the dashboard: full name and a colour per abbreviation.

Colours follow each party's familiar colour where it can (BJP saffron, INC blue, BRS pink),
drawn from the validated reference hues of the dataviz palette. A colour is only ever an
identity cue beside the party's abbreviation, never the sole label; parties without an
entry (and independents) are neutral grey.
"""
from __future__ import annotations

ORANGE, BLUE, AQUA, YELLOW, MAGENTA, GREEN, VIOLET, RED = (
    "#eb6834", "#2a78d6", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948")
NEUTRAL, NOTA_GREY = "#9aa4b2", "#c4cad3"

PARTIES: dict[str, tuple[str, str]] = {
    "BJP": ("Bharatiya Janata Party", ORANGE),
    "INC": ("Indian National Congress", BLUE),
    "BSP": ("Bahujan Samaj Party", VIOLET),
    "SP": ("Samajwadi Party", RED),
    "AAAP": ("Aam Aadmi Party", AQUA),
    "AAP": ("Aam Aadmi Party", AQUA),
    "UKDP": ("Uttarakhand Kranti Dal (P)", YELLOW),
    "UKD": ("Uttarakhand Kranti Dal", YELLOW),
    "TRS": ("Telangana Rashtra Samithi", MAGENTA),
    "BRS": ("Bharat Rashtra Samithi", MAGENTA),
    "BHRS": ("Bharat Rashtra Samithi", MAGENTA),        # as the ECI 2023 report abbreviates it
    "AIMIM": ("All India Majlis-E-Ittehadul Muslimeen", GREEN),
    "TDP": ("Telugu Desam Party", YELLOW),
    "RLD": ("Rashtriya Lok Dal", GREEN),
    "CPI": ("Communist Party of India", RED),
    "CPM": ("Communist Party of India (Marxist)", RED),
    "CPI(M)": ("Communist Party of India (Marxist)", RED),
    "IND": ("Independent", NEUTRAL),
    "NOTA": ("None of the Above", NOTA_GREY),
}


def party_color(abbr: str | None) -> str:
    return PARTIES.get((abbr or "").strip().upper(), ("", NEUTRAL))[1]


def party_name(abbr: str | None) -> str:
    key = (abbr or "").strip()
    return PARTIES.get(key.upper(), (key, ""))[0] or key


# the same party under a later name: a seat it keeps after the rename is a hold, not a gain
RENAMED = {"TRS": {"BRS", "BHRS"}}


def same_party(a: str | None, b: str | None) -> bool:
    a, b = (a or "").strip().upper(), (b or "").strip().upper()
    return a == b or b in RENAMED.get(a, ()) or a in RENAMED.get(b, ())
