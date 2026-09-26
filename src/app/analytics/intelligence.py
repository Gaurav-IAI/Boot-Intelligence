"""Booth Intelligence: deterministic, explainable booth-level aggregates.

Nothing in this module predicts, scores people, or infers political preference.
Every figure is a count or ratio over stored rows, or a documented rule applied
to them. Where a rule cannot be applied honestly it returns None and a reason,
never a plausible-looking substitute.

Two linkage rules are enforced here because the data would otherwise mislead:

* **Form 20 results are never joined to a polling station by part number.**
  2012 booth numbering and SIR-2026 numbering differ (audit finding F-1). A
  result is attached to a station only through a verified mapping; none exists
  in the current POC, so results are shown in their own 2012 numbering.
* **Official 2003->2025 part mappings are verified against SIR-2026 before use.**
  The mapping targets the 2025 roll's part numbers, which were renumbered in
  SIR-2026. A mapping row is `verified` only when its village name appears in
  the same-numbered SIR-2026 station's name or area list; otherwise it is
  `review` and never used as a link.
"""
from __future__ import annotations

import json
import re
import statistics
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import Select, and_, case, func, or_, select
from sqlalchemy.orm import Session

from ..database.models import (
    AcResult, AssemblyConstituency, District, Election, ElectionResult, Elector,
    ElectoralRoll, PartMapping, PollingStation, SourceFetch, State,
)
from ..states import BOOTH_STATE, booth_state_id, mapping_state_id, spec
from .booth import AGE_BANDS, booth_stats, quality_report

EDITION_CURRENT = "SIR-2026"
EDITION_2003 = "ROLL-2003"
DELIM_CURRENT = "current"

LOW_CONF_ROW = 0.95            # same row threshold as analytics.booth
LOW_CONF_BOOTH_MEAN = 0.98     # booth flagged LOW CONFIDENCE below this mean
LOW_CONF_BOOTH_SHARE = 0.05    # ...or when more than 5% of rows are below 0.95
CLOSE_MARGIN_PCT = 5.0         # a booth margin under 5% of valid votes is "close"
MARGIN_BINS = [(0, 5), (5, 10), (10, 20), (20, 30), (30, 50), (50, 101)]

SOURCE_LABELS = {
    "eci_gateway_api": "Election Commission of India — public gateway API",
    "ceo_uk_sir2026_parts": "CEO Uttarakhand — SIR 2026 part list",
    "ceo_uk_ps_list_2026": "CEO Uttarakhand — Polling Station List 2026 (PDF)",
    "ceo_uk_legacy_roll_2003": "CEO Uttarakhand — Electoral Roll 2003 (PDF)",
    "ceo_uk_form20": "CEO Uttarakhand — Form 20 Final Result Sheet (PDF)",
    "ceo_up_form20": "CEO Uttar Pradesh — Form 20 polling-booth-wise result (Excel)",
    "ceo_tg_form20": "CEO Telangana — Form 20 Final Result Sheet (scanned PDF, OCR)",
    "ceo_up_ps_list_2026": "District Election Officer, Uttar Pradesh — List of Polling Stations (SIR 2026)",
    "eci_statistical_report": "Election Commission of India — Statistical Report, Detailed Results",
}


def source_label(code: str | None) -> str:
    return SOURCE_LABELS.get(code or "", code or "Unknown source")


# --- state scope ----------------------------------------------------------
# Every page is scoped to one state. `state_id=None` means the default state (the
# one with booth-level sources), so single-state callers keep their behaviour.
# Results and mappings are keyed by AC *number*, and numbers repeat across states,
# so a number is matched only inside the state that owns the row.
def scope_state_id(db: Session, state_id: int | None) -> int | None:
    return state_id if state_id is not None else booth_state_id(db)


def _state_name(db: Session, state_id: int | None) -> str | None:
    return db.scalar(select(State.state_name).where(State.id == state_id)) if state_id else None


def _scope_state_name(db: Session, state_id: int | None) -> str:
    """The scoped state's name; the default state's name before any state is stored."""
    return _state_name(db, scope_state_id(db, state_id)) or BOOTH_STATE


def _ac_state_id(db: Session, ac: AssemblyConstituency) -> int | None:
    return db.scalar(select(District.state_id).where(District.id == ac.district_id))


def _state_ac_ids(db: Session, state_id: int | None):
    """Subquery: ids of every AC (any delimitation) in a state."""
    return (select(AssemblyConstituency.id)
            .join(District, AssemblyConstituency.district_id == District.id)
            .where(District.state_id == state_id))


def _state_mapping_rows(db: Session, state_id: int | None) -> list[PartMapping]:
    booth = booth_state_id(db)
    return [m for m in db.scalars(select(PartMapping).order_by(
                PartMapping.from_ac_number, PartMapping.from_part_number,
                PartMapping.to_part_number, PartMapping.id))
            if mapping_state_id(m.state_id, booth) == state_id]


def pct(n: float | None, d: float | None) -> float | None:
    return (n / d * 100.0) if (n is not None and d) else None


# ==========================================================================
# Pure calculations (unit-tested without a database)
# ==========================================================================
@dataclass
class Margin:
    leader_votes: int
    runner_up_votes: int
    margin: int
    margin_pct: float | None       # of total valid votes
    leader_share: float | None     # of total valid votes
    columns_with_votes: int


def compute_margin(candidate_votes: list[int] | None, total_valid: int | None,
                   sum_matches: bool | None) -> Margin | None:
    """Leading and second-highest candidate votes at one booth.

    Needs no candidate-name alignment: the top two values of a booth's vote
    columns are the same whichever column they sit in. Only computed when the
    document's own arithmetic check passed (columns sum to total valid votes).
    """
    if not sum_matches or not candidate_votes or len(candidate_votes) < 2:
        return None
    ranked = sorted(candidate_votes, reverse=True)
    m = ranked[0] - ranked[1]
    return Margin(
        leader_votes=ranked[0], runner_up_votes=ranked[1], margin=m,
        margin_pct=round(pct(m, total_valid), 2) if total_valid else None,
        leader_share=round(pct(ranked[0], total_valid), 2) if total_valid else None,
        columns_with_votes=sum(1 for v in candidate_votes if v > 0),
    )


@dataclass
class Change:
    before: int | None
    after: int | None
    delta: int | None
    delta_pct: float | None
    available: bool


def compute_change(before: int | None, after: int | None) -> Change:
    """Observed change between two counts; unavailable if either side is missing."""
    if before is None or after is None:
        return Change(before, after, None, None, False)
    d = after - before
    return Change(before, after, d, round(pct(d, before), 2) if before else None, True)


# --- place-name verification ----------------------------------------------
# Spelling in the published documents varies (काला गांव / कालागाव, नालापानी /
# नालापाणी, खैरी / खेरी), so names are compared after folding marks that do not
# change the place: anusvara/chandrabindu, nukta, long/short vowel pairs, ण/न.
_FOLD = str.maketrans({"ू": "ु", "ी": "ि", "ै": "े", "ौ": "ो", "ण": "न", "ॉ": "ा"})
_DROP = ("‍", "‌", "़", "ँ", "ं", "ॅ")


def _normalize(text: str | None) -> str:
    t = unicodedata.normalize("NFC", text or "")
    for ch in _DROP:
        t = t.replace(ch, "")
    t = t.translate(_FOLD)
    t = re.sub(r"[0-9०-९\-\.,()/:;।\[\]]+", " ", t)
    return " ".join(t.split()).casefold()


def _stem(word: str) -> str:
    return re.sub(r"[ािीुूेैोौ]+$", "", word)


def _norm_set(words: set[str]) -> set[str]:
    return {_stem(_normalize(w)) for w in words}


_PLACE_STOP = _norm_set({"गांव", "गाँव", "ग्राम", "आंशिक"})
_BUILDING_STOP = _norm_set({
    "राजकीय", "प्राथमिक", "विद्यालय", "इंटर", "इण्टर", "कालेज", "कॉलेज", "काॅलेज", "स्कूल",
    "भवन", "रोड", "पब्लिक", "जूनियर", "हाईस्कूल", "उच्चतर", "माध्यमिक", "सेवा", "केन्द",
    "केन्द्र", "पो", "ओ", "तहसील", "जिला", "पिन", "कोड", "देहरादून", "कक्ष", "संख्या",
    "गांव", "ग्राम"})


def place_tokens(place: str | None) -> list[str]:
    return [s for s in (_stem(w) for w in _normalize(place).split())
            if len(s) >= 2 and s not in _PLACE_STOP]


def place_found_in(place: str | None, text: str | None) -> bool:
    """True only if EVERY token of the place name occurs in `text` (spaces ignored)."""
    toks = place_tokens(place)
    hay = _normalize(text).replace(" ", "")
    return bool(toks) and all(t in hay for t in toks)


def building_matches(building: str | None, text: str | None, threshold: float = 0.75) -> bool | None:
    """Does an official building name match a station? None = too generic to be evidence."""
    toks = [t for t in (_stem(w) for w in _normalize(re.sub(r"\(.*", " ", building or "")).split())
            if len(t) >= 3 and t not in _BUILDING_STOP]
    if sum(len(t) for t in toks) < 5:
        return None
    hay = _normalize(text).replace(" ", "")
    return sum(1 for t in toks if t in hay) / len(toks) >= threshold


_PHRASE_STOP = _BUILDING_STOP - _norm_set({"गांव", "ग्राम"})


def building_phrase_in_name(building: str | None, station_name: str | None) -> bool | None:
    """Does the place phrase of an official building name appear as whole words in a station name?

    The 2025 mapping names buildings "generic school + locality" ("राजकीय प्राथमिक
    विद्यालय काला गांव"); the 2026 list prints the locality separately ("काला गांव |
    राजकीय प्राथमिक विद्यालय"). The locality phrase is compared as whole words so
    "काला" never matches inside "कालोनी". None = the name has no usable place phrase.
    """
    words = [w for w in _normalize(re.sub(r"\(.*", " ", building or "")).split()
             if _stem(w) not in _PHRASE_STOP]
    if sum(len(w) for w in words) < 4:
        return None
    return f" {' '.join(words)} " in f" {_normalize(station_name)} "


# --- bridge through the official Polling Station List 2024 (scanned, read by OCR) ---
# The official village mapping names 2025 part numbers; the 2024 list uses the same
# numbering and prints each station's building and villages, which identify the
# station in the renumbered 2026 list. OCR text is noisy, so every link needs the
# row's own serial, the official building name, and a unique, clear current match.
BRIDGE_BUILDING_MIN = 0.5      # 2024 row building/locality vs the mapping's 2025 building name
BRIDGE_MATCH_MIN = 0.6         # combined building + areas similarity to the current station
BRIDGE_MARGIN = 0.15           # lead over the next-best current station
_ROOM = re.compile(r"क\s*[\.\-0]?\s*न\s*[\.\-0ं]*\s*-?\s*(\d)")


def room_number(text: str | None) -> str | None:
    """Room / booth-room number printed as "क.न. 2", "क-नं- 2" or "क0न0 2"."""
    m = _ROOM.search(text or "")
    return m.group(1).translate(str.maketrans("०१२३४५६७८९", "0123456789")) if m else None


def text_overlap(src: str | None, dst: str | None) -> float:
    """Share of the words of `src` (3+ letters, spelling-folded) found in `dst`."""
    words = {t for t in (_stem(w) for w in _normalize(re.sub(r"\(.*", " ", src or "")).split()) if len(t) >= 3}
    hay = _normalize(dst).replace(" ", "")
    return sum(1 for t in words if t in hay) / len(words) if words else 0.0


@dataclass
class BridgeDecision:
    station_part: int | None
    reason: str


def bridge_via_2024_list(to_part_number: int, building_2025: str | None, rows_2024: list[dict],
                         stations: dict[int, tuple[str, str]]) -> BridgeDecision:
    """Link a 2025 part to a current station through the 2024 polling-station list.

    `rows_2024`: OCR rows {serial, locality, building, areas}; `stations`: current
    part -> (station name, areas). Returns station_part=None when any step is not clear.
    """
    rows = [r for r in rows_2024 if r.get("serial") == to_part_number
            and text_overlap(building_2025, f"{r.get('locality', '')} {r.get('building', '')}") >= BRIDGE_BUILDING_MIN]
    if len(rows) != 1:
        return BridgeDecision(None, f"2024 list: {len(rows)} row(s) numbered {to_part_number} with the official "
                                    "building name; no link")
    r = rows[0]
    r_name = f"{r.get('locality', '')} {r.get('building', '')}"
    r_room = room_number(r.get("building"))
    scored = []
    for part, (name, areas) in stations.items():
        s_room = room_number(name)
        if r_room and s_room and r_room != s_room:
            continue
        scored.append((0.5 * text_overlap(r_name, name) + 0.5 * text_overlap(r.get("areas"), areas), part))
    scored.sort(reverse=True)
    if not scored:
        return BridgeDecision(None, "2024 list: no current station to compare")
    best, second = scored[0], (scored[1] if len(scored) > 1 else (0.0, None))
    if best[0] < BRIDGE_MATCH_MIN or best[0] - second[0] < BRIDGE_MARGIN:
        return BridgeDecision(None, f"2024 list: best current match Part {best[1]} scores {best[0]:.2f} "
                                    f"(next {second[0]:.2f}); not clear enough")
    return BridgeDecision(best[1],
                          f"Possible link from the official Polling Station List 2024 (scanned, read by OCR): "
                          f"its Part {to_part_number} shows the building the official mapping names, and its "
                          f"building and villages resemble current Part {best[1]} (score {best[0]:.2f}; next "
                          f"best {second[0]:.2f}). OCR text similarity is not accepted as verification evidence.")


_BRIDGE_CACHE: dict[str, tuple[float, list[dict]]] = {}


def load_ps_list_2024_rows(ac_number: int) -> list[dict] | None:
    """OCR rows of the 2024 list written by the pipeline; None when not produced yet."""
    from ..config import settings
    path = settings.processed_dir / "ocr" / f"ps_list_2024_AC{ac_number}.json"
    if not path.exists():
        return None
    mtime = path.stat().st_mtime
    cached = _BRIDGE_CACHE.get(str(path))
    if cached and cached[0] == mtime:
        return cached[1]
    rows = json.loads(path.read_text(encoding="utf-8")).get("rows", [])
    _BRIDGE_CACHE[str(path)] = (mtime, rows)
    return rows


@dataclass
class MappingDecision:
    status: str                 # verified | review | unmapped
    station_part: int | None    # current part the row is linked to (verified only)
    reason: str
    candidates: list[int]       # unconfirmed possibilities (review only)


def decide_mapping(area_name: str | None, to_part_number: int | None,
                   building_name: str | None, stations: dict[int, str],
                   names: dict[int, str] | None = None,
                   areas: dict[int, str] | None = None) -> MappingDecision:
    """Link one official village mapping row to a current (SIR-2026) polling station.

    The official mapping targets 2025 part numbers, which SIR-2026 renumbered, so
    the number alone proves nothing. A row is verified only when two independent
    official signals agree on ONE current station: the village is listed there AND
    the official 2025 building name matches it. Anything weaker stays `review`.
    """
    if to_part_number is None:
        return MappingDecision("unmapped", None,
                               "The official mapping lists no 2025 part for this village.", [])
    # The village must be in the station's list of areas served — not merely spread
    # across its locality or building name, which several neighbouring booths share.
    village_text = areas if areas is not None else stations
    village_hits = sorted(p for p, t in village_text.items() if place_found_in(area_name, t))
    bmatch = {p: building_matches(building_name, t) for p, t in stations.items()}
    pmatch = ({p: building_phrase_in_name(building_name, names.get(p)) for p in stations}
              if names else {})
    usable = any(v is not None for v in (*bmatch.values(), *pmatch.values()))
    building_hits = sorted(p for p in stations if bmatch.get(p) or pmatch.get(p))
    both = sorted(set(village_hits) & set(building_hits))

    if to_part_number in both:
        return MappingDecision("verified", to_part_number,
                               f"Village '{area_name}' and the official building name both match "
                               f"current Part {to_part_number} (same number as the 2025 roll).", [])
    if len(both) == 1:
        return MappingDecision("verified", both[0],
                               f"Village '{area_name}' and the official building name both match "
                               f"current Part {both[0]}. Renumbered: the 2025 roll called it "
                               f"Part {to_part_number}.", [])

    candidates = both or sorted(set(village_hits) | set(building_hits))
    if both:
        reason = f"Village and building both match several current parts {both}; cannot choose one."
    elif not usable:
        reason = ("The official building name is too generic to confirm a current part"
                  + (f"; the village name appears at Part(s) {village_hits}." if village_hits else "."))
    elif village_hits and building_hits:
        reason = (f"The village name points to Part(s) {village_hits} but the official building "
                  f"name points to Part(s) {building_hits}.")
    elif village_hits:
        reason = (f"Village name found at current Part(s) {village_hits}, but the official "
                  "building name does not match there.")
    elif building_hits:
        reason = (f"Official building name matches current Part(s) {building_hits}, but the "
                  "village is not listed there.")
    else:
        reason = ("Neither the village nor the official building name is found in the current "
                  "polling-station list.")
    return MappingDecision("review", None, reason, candidates if len(candidates) <= 4 else [])


# --- flags and health -----------------------------------------------------
@dataclass
class Flag:
    code: str          # CLEAN, DATA_QUALITY, LOW_CONFIDENCE, ...
    level: str         # good | warn | bad | info
    label: str
    reason: str


@dataclass
class RecordAggregate:
    total: int = 0
    male: int = 0
    female: int = 0
    with_epic: int = 0
    invalid: int = 0
    duplicates: int = 0
    other_invalid: int = 0         # failed a rule other than duplication
    low_conf: int = 0
    mean_confidence: float | None = None
    provenance_complete: int = 0

    @property
    def other_unknown(self) -> int:
        return self.total - self.male - self.female

    @property
    def epic_pct(self) -> float | None:
        return pct(self.with_epic, self.total)

    @property
    def low_conf_share(self) -> float | None:
        return (self.low_conf / self.total) if self.total else None

    @property
    def provenance_pct(self) -> float | None:
        return pct(self.provenance_complete, self.total)

    def add(self, o: "RecordAggregate") -> None:
        n = self.total + o.total
        if n and (self.mean_confidence is not None or o.mean_confidence is not None):
            self.mean_confidence = ((self.mean_confidence or 0) * self.total
                                    + (o.mean_confidence or 0) * o.total) / n
        for f in ("total", "male", "female", "with_epic", "invalid", "duplicates",
                  "other_invalid", "low_conf", "provenance_complete"):
            setattr(self, f, getattr(self, f) + getattr(o, f))


def booth_flags(agg: RecordAggregate | None, *, edition: str,
                mapping_status: str, mapping_note: str,
                historical_status: str, historical_note: str,
                serial_gaps: int = 0, serial_outliers: list[int] | None = None,
                part_number: int | None = None) -> list[Flag]:
    flags: list[Flag] = []
    where = f" in Part {part_number}" if part_number is not None else ""
    if agg is None or agg.total == 0:
        why = ("The current (SIR-2026) roll is not available from the current public source."
               if edition == EDITION_CURRENT else "No electoral roll has been ingested for this booth.")
        flags.append(Flag("NO_ELECTORATE", "info", "No electorate data", why))
    else:
        mean = agg.mean_confidence or 0.0
        share = agg.low_conf_share or 0.0
        low = mean < LOW_CONF_BOOTH_MEAN or share > LOW_CONF_BOOTH_SHARE
        if agg.invalid:
            parts = []
            if agg.duplicates:
                parts.append(f"{agg.duplicates:,} duplicate record(s)")
            if agg.other_invalid:
                parts.append(f"{agg.other_invalid:,} other validation flag(s) "
                             "(EPIC format / gender / field rules)")
            flags.append(Flag("DATA_QUALITY", "warn", "Data quality review",
                              "; ".join(parts) + f" detected{where}. Records are kept "
                              "and flagged, never deleted."))
        if low:
            flags.append(Flag("LOW_CONFIDENCE", "warn", "Low confidence",
                              f"Mean extraction confidence {mean * 100:.2f}% "
                              f"(threshold {LOW_CONF_BOOTH_MEAN * 100:.0f}%); "
                              f"{agg.low_conf:,} rows below {LOW_CONF_ROW * 100:.0f}%."))
        if not agg.invalid and not low:
            flags.append(Flag("CLEAN", "good", "Clean",
                              f"0 validation flags, 0 duplicates, mean extraction "
                              f"confidence {mean * 100:.2f}%."))
        if serial_outliers:
            flags.append(Flag("SOURCE_ANOMALY", "warn", "Source anomaly",
                              f"Stray serial(s) {serial_outliers} printed in the source "
                              "document; excluded from the derived count basis, record kept."))
        if serial_gaps:
            flags.append(Flag("SERIAL_GAPS", "info", "Serial gaps",
                              f"{serial_gaps} serial number(s) are not printed in the source "
                              "roll (normally deleted electors)."))

    mapping_flag = {
        "verified": ("MAPPING_VERIFIED", "good", "Mapping verified"),
        "review": ("MAPPING_REVIEW", "warn", "Mapping review required"),
        "none": ("NO_MAPPING", "info", "No mapping"),
    }[mapping_status]
    flags.append(Flag(*mapping_flag, mapping_note))

    hist_flag = {
        "linked": ("HISTORICAL_AVAILABLE", "good", "Historical result available"),
        "unlinked": ("HISTORICAL_UNLINKED", "warn", "Historical result — mapping required"),
        "none": ("HISTORICAL_MISSING", "info", "Historical result missing"),
    }[historical_status]
    flags.append(Flag(*hist_flag, historical_note))
    return flags


@dataclass
class HealthComponent:
    name: str
    score: float
    max: int
    reason: str


@dataclass
class Health:
    total: int
    components: list[HealthComponent]

    @property
    def band(self) -> str:
        return "Strong" if self.total >= 75 else "Partial" if self.total >= 50 else "Limited"

    @property
    def level(self) -> str:
        return "good" if self.total >= 75 else "warn" if self.total >= 50 else "info"


def booth_health(agg: RecordAggregate | None, *, station_has_provenance: bool,
                 mapping_status: str, historical_linked: bool) -> Health:
    """Booth Data Health, 0-100: how complete, verified and traceable a booth's data is.

    Not a political indicator of any kind. Components and weights:
      Electorate data 20 · Record validation 20 · Extraction confidence 15 ·
      Provenance 15 · Historical result link 15 · Official mapping 15
    """
    c: list[HealthComponent] = []
    n = agg.total if agg else 0
    if n:
        c.append(HealthComponent("Electorate data", 20, 20,
                                 f"{n:,} elector records extracted from the official roll."))
        q = 20 * max(0.0, 1 - 10 * agg.invalid / n)
        c.append(HealthComponent("Record validation", q, 20,
                                 f"{agg.invalid:,} of {n:,} records flagged "
                                 f"({agg.invalid / n * 100:.2f}%); each 1% flagged costs 2 points."))
        e = 15 * max(0.0, 1 - 5 * agg.low_conf / n)
        c.append(HealthComponent("Extraction confidence", e, 15,
                                 f"{agg.low_conf:,} rows below {LOW_CONF_ROW * 100:.0f}% confidence "
                                 f"({agg.low_conf / n * 100:.2f}%); each 1% costs 0.75 points."))
        p = 15 * (agg.provenance_complete / n)
        c.append(HealthComponent("Provenance", p, 15,
                                 f"{agg.provenance_complete:,} of {n:,} records carry source URL, "
                                 "document, page, raw text, parser version and method."))
    else:
        c.append(HealthComponent("Electorate data", 0, 20,
                                 "No elector records for this booth in the current POC."))
        c.append(HealthComponent("Record validation", 0, 20, "Not assessable — no records."))
        c.append(HealthComponent("Extraction confidence", 0, 15, "Not assessable — no records."))
        c.append(HealthComponent("Provenance", 15 if station_has_provenance else 0, 15,
                                 "Station-level source and URL recorded."
                                 if station_has_provenance else "Station source/URL missing."))
    c.append(HealthComponent("Historical result link", 15 if historical_linked else 0, 15,
                             "A historical result is linked through a verified mapping."
                             if historical_linked else
                             "No historical result linked through a verified mapping."))
    ms = {"verified": 15, "review": 5, "none": 0}[mapping_status]
    c.append(HealthComponent("Official mapping", ms, 15, {
        "verified": "Official 2003↔current part mapping verified against SIR-2026.",
        "review": "Official mapping exists but needs verification against SIR-2026.",
        "none": "No official part mapping ingested for this booth.",
    }[mapping_status]))
    for comp in c:
        comp.score = round(comp.score, 1)
    return Health(total=int(round(sum(x.score for x in c))), components=c)


def margin_histogram(margin_pcts: list[float]) -> list[dict]:
    out = []
    for lo, hi in MARGIN_BINS:
        n = sum(1 for m in margin_pcts if lo <= m < hi)
        out.append({"label": f"{lo}–{hi}%" if hi <= 100 else f"{lo}%+", "count": n})
    out[-1]["label"] = f"{MARGIN_BINS[-1][0]}%+"
    return out


# ==========================================================================
# Database-backed services
# ==========================================================================
def _cnt(cond):
    return func.coalesce(func.sum(case((cond, 1), else_=0)), 0)


def record_aggregates(db: Session, station_ids: list[int] | Select | None = None
                      ) -> dict[int, RecordAggregate]:
    prov = and_(Elector.source_url.is_not(None), Elector.source_document.is_not(None),
                Elector.source_page.is_not(None), Elector.raw_text.is_not(None),
                Elector.parser_version.is_not(None), Elector.extraction_method.is_not(None))
    stmt = select(
        Elector.polling_station_id, func.count(),
        _cnt(Elector.gender == "M"), _cnt(Elector.gender == "F"),
        _cnt(and_(Elector.epic_number.is_not(None), Elector.epic_number != "")),
        _cnt(Elector.is_valid.is_(False)), _cnt(Elector.duplicate_kind.is_not(None)),
        _cnt(and_(Elector.is_valid.is_(False), Elector.duplicate_kind.is_(None))),
        _cnt(Elector.extraction_confidence < LOW_CONF_ROW),
        func.avg(Elector.extraction_confidence), _cnt(prov),
    ).group_by(Elector.polling_station_id)
    if station_ids is not None:
        stmt = stmt.where(Elector.polling_station_id.in_(station_ids))
    out = {}
    for sid, tot, m, f, epic, inv, dup, oth, low, avg, pv in db.execute(stmt):
        out[sid] = RecordAggregate(
            total=tot, male=int(m), female=int(f), with_epic=int(epic), invalid=int(inv),
            duplicates=int(dup), other_invalid=int(oth), low_conf=int(low),
            mean_confidence=float(avg) if avg is not None else None,
            provenance_complete=int(pv))
    return out


# --- mappings -------------------------------------------------------------
@dataclass
class MappingView:
    id: int
    from_edition: str
    from_ac_number: int
    from_ac_name: str | None
    from_part_number: int
    from_part_name: str | None
    area_name: str | None
    to_edition: str
    to_ac_number: int | None
    to_ac_name: str | None
    to_part_number: int | None
    to_part_name: str | None
    method: str
    confidence: float
    source: str
    status: str
    reason: str
    linked_station_id: int | None = None          # only when verified
    linked_station_name: str | None = None
    linked_part_number: int | None = None
    same_number_station_id: int | None = None
    same_number_station_name: str | None = None
    suggestions: list[dict] = field(default_factory=list)   # name matches, unverified
    from_station_id: int | None = None


def _station_text(s: PollingStation) -> str:
    return " ".join(x for x in (s.polling_station_name_local, s.area_description,
                                s.polling_station_name) if x)


def station_label(s: PollingStation) -> str:
    return s.polling_station_name_local or s.polling_station_name or s.part_name or ""


def evaluate_mappings(db: Session, state_id: int | None = None) -> list[MappingView]:
    """The official part mappings of one state (default: the booth state), each checked
    against that state's SIR-2026 stations."""
    sid = scope_state_id(db, state_id)
    rows = _state_mapping_rows(db, sid)
    if not rows:
        return []
    in_state = PollingStation.ac_id.in_(_state_ac_ids(db, sid))
    # current-delimitation SIR-2026 stations, keyed by AC number
    cur: dict[int, list[PollingStation]] = {}
    for s, acn in db.execute(
            select(PollingStation, AssemblyConstituency.ac_number)
            .join(AssemblyConstituency, PollingStation.ac_id == AssemblyConstituency.id)
            .where(AssemblyConstituency.delimitation == DELIM_CURRENT,
                   PollingStation.edition == EDITION_CURRENT, in_state)):
        cur.setdefault(acn, []).append(s)
    old: dict[tuple[int, int], int] = {
        (acn, s.part_number): s.id for s, acn in db.execute(
            select(PollingStation, AssemblyConstituency.ac_number)
            .join(AssemblyConstituency, PollingStation.ac_id == AssemblyConstituency.id)
            .where(PollingStation.edition == EDITION_2003, in_state))}

    texts = {acn: {s.part_number: _station_text(s) for s in lst} for acn, lst in cur.items()}
    names = {acn: {s.part_number: s.polling_station_name_local or "" for s in lst}
             for acn, lst in cur.items()}
    areas = {acn: {s.part_number: s.area_description or "" for s in lst}
             for acn, lst in cur.items()}
    by_part = {acn: {s.part_number: s for s in lst} for acn, lst in cur.items()}
    out = []
    seen: set[tuple] = set()
    for m in rows:
        parts = by_part.get(m.to_ac_number, {})
        same = parts.get(m.to_part_number)
        d = decide_mapping(m.area_name, m.to_part_number, m.to_part_name,
                           texts.get(m.to_ac_number, {}), names.get(m.to_ac_number, {}),
                           areas.get(m.to_ac_number, {}))
        if d.status == "review" and m.to_part_number is not None and m.to_ac_number is not None:
            rows_2024 = load_ps_list_2024_rows(m.to_ac_number)
            if rows_2024:
                fields = {p: (names[m.to_ac_number].get(p, ""), areas[m.to_ac_number].get(p, ""))
                          for p in by_part.get(m.to_ac_number, {})}
                b = bridge_via_2024_list(m.to_part_number, m.to_part_name, rows_2024, fields)
                # OCR evidence only ever narrows the suggestion; it never makes a row Verified.
                if b.station_part is not None:
                    d = MappingDecision("review", None, f"{d.reason} {b.reason}", [b.station_part])
                else:
                    d = MappingDecision("review", None, f"{d.reason} {b.reason}.", d.candidates)
        key = (m.from_edition, m.from_ac_number, m.from_part_number, _normalize(m.area_name),
               m.to_ac_number, m.to_part_number)
        if key in seen:
            continue                     # the same official relationship stored twice counts once
        seen.add(key)
        status, reason = d.status, d.reason
        mv = MappingView(
            id=m.id, from_edition=m.from_edition, from_ac_number=m.from_ac_number,
            from_ac_name=m.from_ac_name, from_part_number=m.from_part_number,
            from_part_name=m.from_part_name, area_name=m.area_name,
            to_edition=m.to_edition, to_ac_number=m.to_ac_number, to_ac_name=m.to_ac_name,
            to_part_number=m.to_part_number, to_part_name=m.to_part_name,
            method=m.mapping_method, confidence=m.confidence, source=m.source,
            status=status, reason=reason,
            same_number_station_id=same.id if same else None,
            same_number_station_name=station_label(same) if same else None,
            from_station_id=old.get((m.from_ac_number, m.from_part_number)))
        if status == "verified":
            st = parts[d.station_part]
            mv.linked_station_id, mv.linked_station_name = st.id, station_label(st)
            mv.linked_part_number = d.station_part
        elif status == "review":
            mv.suggestions = [{"station_id": parts[p].id, "part_number": p,
                               "name": station_label(parts[p])} for p in d.candidates]
        out.append(mv)
    return out


def _mapping_status_for_station(st: PollingStation, ac: AssemblyConstituency,
                                maps: list[MappingView]) -> tuple[str, str, list[MappingView]]:
    if st.edition == EDITION_2003:
        mine = [m for m in maps if m.from_edition == st.edition
                and m.from_ac_number == ac.ac_number and m.from_part_number == st.part_number]
        if not mine:
            return "none", "No official 2003→current mapping ingested for this part.", mine
        v = sum(1 for m in mine if m.status == "verified")
        r = sum(1 for m in mine if m.status == "review")
        if v:
            return ("verified", f"{v} of {len(mine)} village mapping(s) verified against "
                                f"SIR-2026; {len(mine) - v} not confirmed.", mine)
        if r:
            return ("review", f"{r} official village mapping(s) exist but none could be "
                              "confirmed against the current polling-station list.", mine)
        return "none", "The official mapping lists no current part for this part's villages.", mine
    mine = [m for m in maps if m.linked_station_id == st.id]
    if mine:
        return ("verified", f"{len(mine)} village(s) from 2003 Part(s) "
                            f"{sorted({m.from_part_number for m in mine})} verified at this station.",
                mine)
    sugg = [m for m in maps if any(s["station_id"] == st.id for s in m.suggestions)]
    if sugg:
        return ("review", f"Village name(s) {[m.area_name for m in sugg]} from an official "
                          "2003 mapping appear at this station, but the mapped part number "
                          "differs — verification required.", sugg)
    return "none", "No official 2003 mapping links to this station.", []


# --- historical results ---------------------------------------------------
@dataclass
class ResultView:
    id: int
    year: int
    election_type: str
    ac_number: int | None
    part_number: int | None
    station_name: str | None
    total_votes: int | None
    total_valid_votes: int | None
    tendered_votes: int | None
    rejected_votes: int | None
    nota_votes: int | None
    columns: list[int] | None
    sum_matches: bool | None
    margin: Margin | None
    reliability: str          # ok | check_failed | not_backfilled
    reliability_note: str
    source: str
    source_url: str | None
    source_file: str | None
    source_page: int | None
    parser_version: str | None
    confidence: float | None
    # [{"name", "party"}] in vote-column order, where the source prints names legibly
    candidates: list[dict] | None = None

    @property
    def verified(self) -> bool:
        return self.reliability == "ok"

    @property
    def named_columns(self) -> list[tuple[dict, int]] | None:
        """(candidate, votes) pairs — only for a verified row whose names align one-to-one
        with its vote columns; otherwise names are not attributed."""
        if not (self.verified and self.candidates and self.columns
                and len(self.candidates) == len(self.columns)):
            return None
        return list(zip(self.candidates, self.columns))

    def _ranked_name(self, rank: int) -> dict | None:
        pairs = self.named_columns
        if not pairs or len(pairs) <= rank:
            return None
        ranked = sorted(pairs, key=lambda p: p[1], reverse=True)
        if ranked[rank][1] in {v for i, (_, v) in enumerate(ranked) if i != rank}:
            return None                   # tied with another column: no single name
        return ranked[rank][0]

    @property
    def leader(self) -> dict | None:
        return self._ranked_name(0)

    @property
    def runner_up(self) -> dict | None:
        return self._ranked_name(1)


def expected_column_count(counts: list[int]) -> int | None:
    """A result sheet's vote-column count: the count shared by more than half of its rows."""
    if not counts:
        return None
    n, k = Counter(counts).most_common(1)[0]
    return n if k * 2 > len(counts) else None


def form20_row_check(columns: list[int] | None, total_valid: int | None,
                     expected_columns: int | None = None) -> tuple[str, str]:
    """The one rule that decides whether a Form 20 row is verified.

    Recomputed from the stored vote columns on every call — the `vote_sum_matches`
    flag written at ingestion is not trusted on its own. A row is verified only if
    its columns sum exactly to the printed total valid votes AND it has the same
    number of vote columns as the rest of its result sheet (a blank or unread cell
    keeps the sum but shifts every later column onto the wrong candidate).
    Returns (reliability, note): ok | check_failed | not_backfilled.
    """
    if columns is None:
        return "not_backfilled", "Candidate vote columns are not stored, so this row cannot be checked."
    if total_valid is None or sum(columns) != total_valid:
        printed = f"{total_valid:,}" if total_valid is not None else "not read"
        return "check_failed", (f"The extracted vote columns (sum {sum(columns):,}) do not reconcile with "
                                f"the printed total valid votes ({printed}).")
    if expected_columns and len(columns) != expected_columns:
        return "check_failed", ("One candidate cell is blank/missing, so candidate-column alignment cannot be "
                                f"established safely ({len(columns)} vote columns read; the result sheet has "
                                f"{expected_columns}).")
    return "ok", ("Vote columns sum exactly to the printed total valid votes, and the column count "
                  "matches the result sheet.")


def _result_view(r: ElectionResult, e: Election, expected_columns: int | None) -> ResultView:
    cols = json.loads(r.candidate_votes_json) if r.candidate_votes_json else None
    rel, note = form20_row_check(cols, r.total_valid_votes, expected_columns)
    return ResultView(
        id=r.id, year=e.election_year, election_type=e.election_type,
        ac_number=r.ac_number, part_number=r.part_number,
        station_name=r.polling_station_name, total_votes=r.total_votes,
        total_valid_votes=r.total_valid_votes, tendered_votes=r.tendered_votes,
        rejected_votes=r.rejected_votes, nota_votes=r.nota_votes, columns=cols,
        sum_matches=(cols is not None and r.total_valid_votes is not None
                     and sum(cols) == r.total_valid_votes),
        margin=compute_margin(cols, r.total_valid_votes, rel == "ok"),
        reliability=rel, reliability_note=note, source=r.source,
        source_url=r.source_url, source_file=r.source_file, source_page=r.source_page,
        parser_version=r.parser_version, confidence=r.extraction_confidence,
        candidates=json.loads(r.candidates_json) if r.candidates_json else None)


def result_views(pairs) -> list[ResultView]:
    """ResultViews for (ElectionResult, Election) pairs, each checked against its own sheet."""
    pairs = list(pairs)
    counts: dict[tuple, list[int]] = {}
    for r, _ in pairs:
        if r.candidate_votes_json:
            counts.setdefault((r.election_id, r.ac_number), []).append(len(json.loads(r.candidate_votes_json)))
    expected = {k: expected_column_count(v) for k, v in counts.items()}
    return [_result_view(r, e, expected.get((r.election_id, r.ac_number))) for r, e in pairs]


def results_for_ac(db: Session, ac: AssemblyConstituency) -> list[ResultView]:
    """Form 20 rows for an AC. Post-2008 elections only join to `current` ACs."""
    if ac.delimitation != DELIM_CURRENT:
        return []
    stmt = (select(ElectionResult, Election)
            .join(Election, ElectionResult.election_id == Election.id)
            .where(Election.state == _state_name(db, _ac_state_id(db, ac)),
                   or_(ElectionResult.ac_id == ac.id,
                       and_(ElectionResult.ac_id.is_(None),
                            ElectionResult.ac_number == ac.ac_number)))
            .order_by(Election.election_year, ElectionResult.part_number))
    return result_views(db.execute(stmt))


def all_result_views(db: Session, state_id: int | None = None) -> list[ResultView]:
    return result_views(db.execute(select(ElectionResult, Election)
                                   .join(Election, ElectionResult.election_id == Election.id)
                                   .where(Election.state == _scope_state_name(db, state_id))
                                   .order_by(Election.election_year, ElectionResult.ac_number,
                                             ElectionResult.part_number)))


@dataclass(slots=True)
class ResultCheck:
    """The verification outcome of one Form 20 row, without the display fields — for counts
    over a whole state (hundreds of thousands of rows)."""
    id: int
    year: int
    ac_number: int | None
    part_number: int | None
    reliability: str
    margin: Margin | None
    named: bool

    @property
    def verified(self) -> bool:
        return self.reliability == "ok"


def result_checks(db: Session, state_id: int | None = None) -> list[ResultCheck]:
    """`form20_row_check` and `compute_margin` over a state's rows — the same rule and the same
    sheet column counts as `result_views`, reading only the columns the rule needs."""
    rows = db.execute(select(ElectionResult.id, ElectionResult.election_id, Election.election_year,
                             ElectionResult.ac_number, ElectionResult.part_number,
                             ElectionResult.candidate_votes_json, ElectionResult.total_valid_votes,
                             ElectionResult.candidates_json.is_not(None))
                      .join(Election, ElectionResult.election_id == Election.id)
                      .where(Election.state == _scope_state_name(db, state_id))
                      .order_by(Election.election_year, ElectionResult.ac_number,
                                ElectionResult.part_number)).all()
    cols = [json.loads(r[5]) if r[5] else None for r in rows]
    counts: dict[tuple, list[int]] = {}
    for r, c in zip(rows, cols):
        if c is not None:
            counts.setdefault((r[1], r[3]), []).append(len(c))
    expected = {k: expected_column_count(v) for k, v in counts.items()}
    out = []
    for r, c in zip(rows, cols):
        rel, _ = form20_row_check(c, r[6], expected.get((r[1], r[3])))
        out.append(ResultCheck(id=r[0], year=r[2], ac_number=r[3], part_number=r[4], reliability=rel,
                               margin=compute_margin(c, r[6], rel == "ok"), named=bool(r[7])))
    return out


def result_detail(db: Session, result_id: int) -> tuple[ResultView, AssemblyConstituency | None] | None:
    row = db.execute(select(ElectionResult, Election)
                     .join(Election, ElectionResult.election_id == Election.id)
                     .where(ElectionResult.id == result_id)).first()
    if row is None:
        return None
    r, e = row
    sheet = db.scalars(select(ElectionResult.candidate_votes_json).where(
        ElectionResult.election_id == r.election_id, ElectionResult.ac_number == r.ac_number,
        ElectionResult.candidate_votes_json.is_not(None))).all()
    expected = expected_column_count([len(json.loads(j)) for j in sheet])
    ac = db.get(AssemblyConstituency, r.ac_id) if r.ac_id else db.scalar(
        select(AssemblyConstituency)
        .join(District, AssemblyConstituency.district_id == District.id)
        .join(State, District.state_id == State.id)
        .where(State.state_name == e.state, AssemblyConstituency.ac_number == r.ac_number,
               AssemblyConstituency.delimitation == DELIM_CURRENT))
    return _result_view(r, e, expected), ac


@dataclass
class AcElectionView:
    """One constituency's result in one election, from the ECI statistical report."""
    year: int
    ac_number: int
    ac_name: str | None
    electors: int | None
    candidates: list[AcResult]          # highest total first
    verified: bool
    note: str
    source_url: str | None
    source_file: str | None

    @property
    def total(self) -> int:
        return sum(c.total_votes for c in self.candidates)

    @property
    def winner(self) -> AcResult | None:
        return self.candidates[0] if self.candidates else None

    @property
    def runner_up(self) -> AcResult | None:
        return self.candidates[1] if len(self.candidates) > 1 else None

    @property
    def margin(self) -> int | None:
        return (self.winner.total_votes - self.runner_up.total_votes) if self.runner_up else None

    @property
    def margin_pct(self) -> float | None:
        return round(pct(self.margin, self.total), 2) if self.margin is not None and self.total else None

    @property
    def turnout_pct(self) -> float | None:
        return round(pct(self.total, self.electors), 2) if self.electors else None


def ac_result_check(rows: list[AcResult]) -> tuple[bool, str]:
    """Recomputed on every read: general + postal = total for each candidate, and the candidates
    add up to the report's printed constituency total where one is printed."""
    bad = [r.candidate_name for r in rows if r.general_votes is not None and r.postal_votes is not None
           and r.general_votes + r.postal_votes != r.total_votes]
    if bad:
        return False, f"General + postal votes differ from the total for {', '.join(bad[:3])}."
    printed = next((r.printed_ac_total for r in rows if r.printed_ac_total is not None), None)
    total = sum(r.total_votes for r in rows)
    if printed is not None and printed != total:
        return False, f"Candidate votes sum to {total:,}; the report prints {printed:,}."
    return True, ("Candidate votes add up to the constituency total printed in the ECI report."
                  if printed is not None else
                  "General + postal votes equal the total for every candidate in the ECI report.")


def ac_level_results(db: Session, ac: AssemblyConstituency) -> list[AcElectionView]:
    """Constituency-level results (ECI statistical report) for a current AC, oldest first."""
    if ac.delimitation != DELIM_CURRENT:
        return []
    rows = db.execute(select(AcResult, Election).join(Election, AcResult.election_id == Election.id)
                      .where(or_(AcResult.ac_id == ac.id,
                                 and_(AcResult.ac_id.is_(None), AcResult.ac_number == ac.ac_number,
                                      Election.state == _state_name(db, _ac_state_id(db, ac)))))
                      .order_by(Election.election_year)).all()
    by_year: dict[int, list[AcResult]] = {}
    for r, e in rows:
        by_year.setdefault(e.election_year, []).append(r)
    out = []
    for year, rs in sorted(by_year.items()):
        ok, note = ac_result_check(rs)
        rs = sorted(rs, key=lambda r: -r.total_votes)
        out.append(AcElectionView(year=year, ac_number=rs[0].ac_number, ac_name=rs[0].ac_name,
                                  electors=rs[0].total_electors, candidates=rs, verified=ok, note=note,
                                  source_url=rs[0].source_url, source_file=rs[0].source_file))
    return out


@dataclass
class ElectionSummary:
    year: int
    election_type: str
    booths: int
    total_votes: int
    total_valid: int
    booths_with_margin: int
    sum_check_pass: int
    sum_check_fail: list[int]
    median_margin: float | None
    median_margin_pct: float | None
    close_booths: int
    narrowest: ResultView | None
    widest: ResultView | None
    histogram: list[dict]
    not_backfilled: int
    zero_margin: int = 0

    @property
    def needs_review(self) -> int:
        return self.booths - self.sum_check_pass


def form20_quality_summary(results: list[ResultView]) -> dict:
    """Form 20 counts for any set of rows — the single source for every page and export.

    verified + review == rows always holds: a row is verified only by
    `form20_row_check`; anything else (failed or unchecked) requires review.
    Zero-margin rows are counted among verified rows only.
    """
    review = [r for r in results if not r.verified]
    by_ac: dict[tuple[int, int | None], list[int]] = {}
    for r in review:
        by_ac.setdefault((r.year, r.ac_number), []).append(r.part_number)
    return {"rows": len(results), "verified": len(results) - len(review), "review": len(review),
            "review_parts": sorted(r.part_number for r in review if r.part_number is not None),
            "review_by_ac": {k: sorted(p for p in v if p is not None) for k, v in sorted(by_ac.items())},
            "zero_margin": sum(1 for r in results if r.verified and r.margin and r.margin.margin == 0),
            "acs": len({r.ac_number for r in results})}


def summarize_results(results: list[ResultView]) -> list[ElectionSummary]:
    by_year: dict[tuple[int, str], list[ResultView]] = {}
    for r in results:
        by_year.setdefault((r.year, r.election_type), []).append(r)
    out = []
    for (year, typ), rs in sorted(by_year.items()):
        q = form20_quality_summary(rs)
        with_m = [r for r in rs if r.verified and r.margin]
        mpcts = [r.margin.margin_pct for r in with_m if r.margin.margin_pct is not None]
        out.append(ElectionSummary(
            year=year, election_type=typ, booths=q["rows"],
            total_votes=sum(r.total_votes or 0 for r in rs),
            total_valid=sum(r.total_valid_votes or 0 for r in rs),
            booths_with_margin=len(with_m),
            sum_check_pass=q["verified"],
            sum_check_fail=q["review_parts"],
            median_margin=statistics.median(r.margin.margin for r in with_m) if with_m else None,
            median_margin_pct=round(statistics.median(mpcts), 2) if mpcts else None,
            close_booths=sum(1 for m in mpcts if m < CLOSE_MARGIN_PCT),
            narrowest=min(with_m, key=lambda r: r.margin.margin) if with_m else None,
            widest=max(with_m, key=lambda r: r.margin.margin) if with_m else None,
            histogram=margin_histogram(mpcts),
            not_backfilled=sum(1 for r in rs if r.reliability == "not_backfilled"),
            zero_margin=q["zero_margin"],
        ))
    return out


# --- booth rows -----------------------------------------------------------
@dataclass
class BoothRow:
    station_id: int
    part_number: int
    name: str | None
    name_local: str | None
    areas: str | None
    edition: str
    source: str | None
    has_roll: bool
    agg: RecordAggregate | None
    stats: object | None             # analytics.booth.BoothStats for ingested rolls
    mapping_status: str
    mapping_note: str
    mappings: list[MappingView]
    historical_status: str
    historical_note: str
    flags: list[Flag]
    health: Health

    @property
    def flag_codes(self) -> str:
        return " ".join(f.code for f in self.flags)


def _historical_status(ac: AssemblyConstituency, st: PollingStation,
                       results: list[ResultView]) -> tuple[str, str]:
    if results:
        years = sorted({r.year for r in results})
        return ("unlinked", f"Form 20 {', '.join(map(str, years))} holds {len(results)} booth "
                            f"results for AC {ac.ac_number} in that election's own booth "
                            "numbering. No verified booth mapping to "
                            f"{st.edition} exists, so no result is attached to this booth "
                            "(matching by part number would be wrong).")
    if st.edition == EDITION_2003:
        return ("none", "No Form 20 result for the 2003-delimitation constituency is "
                        "ingested in the current POC.")
    return "none", "No historical Form 20 result ingested for this constituency."


def build_booth_rows(db: Session, ac: AssemblyConstituency,
                     stations: list[PollingStation],
                     maps: list[MappingView] | None = None,
                     results: list[ResultView] | None = None) -> list[BoothRow]:
    maps = evaluate_mappings(db, _ac_state_id(db, ac)) if maps is None else maps
    results = results_for_ac(db, ac) if results is None else results
    aggs = record_aggregates(db, [s.id for s in stations])
    rolls = {r.polling_station_id: r for r in db.scalars(select(ElectoralRoll).where(
        ElectoralRoll.polling_station_id.in_([s.id for s in stations])))}
    rows = []
    for st in stations:
        agg = aggs.get(st.id)
        roll = rolls.get(st.id)
        bs = booth_stats(db, roll) if roll is not None and agg else None
        mstatus, mnote, mine = _mapping_status_for_station(st, ac, maps)
        hstatus, hnote = _historical_status(ac, st, results)
        flags = booth_flags(
            agg, edition=st.edition, mapping_status=mstatus, mapping_note=mnote,
            historical_status=hstatus, historical_note=hnote,
            serial_gaps=bs.serial_gaps if bs else 0,
            serial_outliers=bs.serial_outliers if bs else None,
            part_number=st.part_number)
        health = booth_health(agg, station_has_provenance=bool(st.source and st.source_url),
                              mapping_status=mstatus, historical_linked=hstatus == "linked")
        rows.append(BoothRow(
            station_id=st.id, part_number=st.part_number, name=st.polling_station_name,
            name_local=st.polling_station_name_local, areas=st.area_description,
            edition=st.edition, source=st.source, has_roll=roll is not None,
            agg=agg, stats=bs, mapping_status=mstatus, mapping_note=mnote, mappings=mine,
            historical_status=hstatus, historical_note=hnote, flags=flags, health=health))
    return rows


# --- AC overview ----------------------------------------------------------
@dataclass
class AcOverview:
    ac: AssemblyConstituency
    district: District
    state: State | None
    rows: list[BoothRow]
    totals: RecordAggregate
    stations: int
    stations_with_roll: int
    editions: list[str]
    elections: list[ElectionSummary]
    results: list[ResultView]
    mapping_verified: int
    mapping_review: int
    related_acs: list[dict]          # the other delimitation linked by official mapping

    @property
    def coverage_pct(self) -> float | None:
        return pct(self.stations_with_roll, self.stations)


def ac_overview(db: Session, ac_id: int) -> AcOverview | None:
    ac = db.get(AssemblyConstituency, ac_id)
    if ac is None:
        return None
    district = db.get(District, ac.district_id)
    state = db.get(State, district.state_id) if district else None
    stations = db.scalars(select(PollingStation).where(PollingStation.ac_id == ac_id)
                          .order_by(PollingStation.part_number)).all()
    maps = evaluate_mappings(db, district.state_id)
    results = results_for_ac(db, ac)
    rows = build_booth_rows(db, ac, stations, maps, results)
    totals = RecordAggregate()
    for r in rows:
        if r.agg:
            totals.add(r.agg)
    if ac.delimitation == DELIM_CURRENT:
        relevant = [m for m in maps if m.to_ac_number == ac.ac_number]
        related = {(m.from_ac_number, m.from_ac_name) for m in relevant}
        rel_delim = "2003"
    else:
        relevant = [m for m in maps if m.from_ac_number == ac.ac_number
                    and m.from_edition == EDITION_2003]
        related = {(m.to_ac_number, m.to_ac_name) for m in relevant}
        rel_delim = DELIM_CURRENT
    related_acs = []
    for num, name in sorted(related, key=lambda x: (x[0] or 0)):
        other = db.scalar(select(AssemblyConstituency).where(
            AssemblyConstituency.id.in_(_state_ac_ids(db, district.state_id)),
            AssemblyConstituency.ac_number == num,
            AssemblyConstituency.delimitation == rel_delim))
        related_acs.append({"ac_number": num, "name": name, "delimitation": rel_delim,
                            "id": other.id if other else None})
    return AcOverview(
        ac=ac, district=district, state=state, rows=rows, totals=totals,
        stations=len(stations), stations_with_roll=sum(1 for r in rows if r.agg),
        editions=sorted({s.edition for s in stations}),
        elections=summarize_results(results), results=results,
        mapping_verified=sum(1 for m in relevant if mapping_status_label(m.status) == "Verified"),
        mapping_review=sum(1 for m in relevant if mapping_status_label(m.status) == "Review Required"),
        related_acs=related_acs)


# --- booth detail ---------------------------------------------------------
@dataclass
class BoothDetail:
    station: PollingStation
    ac: AssemblyConstituency
    district: District
    state: State | None
    row: BoothRow
    roll: ElectoralRoll | None
    predecessors: list[dict]      # verified 2003 parts feeding this current booth
    successors: list[dict]        # verified current booths fed by this 2003 part
    ac_results: list[ElectionSummary]
    provenance_fields: list[tuple[str, int]]


def booth_detail(db: Session, station_id: int) -> BoothDetail | None:
    st = db.get(PollingStation, station_id)
    if st is None:
        return None
    ac = db.get(AssemblyConstituency, st.ac_id)
    district = db.get(District, ac.district_id)
    state = db.get(State, district.state_id) if district else None
    maps = evaluate_mappings(db, district.state_id)
    results = results_for_ac(db, ac)
    row = build_booth_rows(db, ac, [st], maps, results)[0]
    roll = db.scalar(select(ElectoralRoll).where(ElectoralRoll.polling_station_id == st.id))

    predecessors, successors = [], []
    if st.edition == EDITION_CURRENT:
        by_part: dict[int, list[MappingView]] = {}
        for m in row.mappings:
            if m.status == "verified":
                by_part.setdefault(m.from_part_number, []).append(m)
        for part, ms in sorted(by_part.items()):
            sid = ms[0].from_station_id
            agg = record_aggregates(db, [sid]).get(sid) if sid else None
            old = db.get(PollingStation, sid) if sid else None
            predecessors.append({"part_number": part, "ac_number": ms[0].from_ac_number,
                                 "ac_name": ms[0].from_ac_name, "station_id": sid,
                                 "station_name": station_label(old) if old else None,
                                 "villages": [m.area_name for m in ms], "agg": agg})
    else:
        for m in row.mappings:
            successors.append(m)

    prov = []
    if roll is not None:
        base = select(func.count()).select_from(Elector).where(Elector.electoral_roll_id == roll.id)
        for label, col in (("Source URL", Elector.source_url), ("Source document", Elector.source_document),
                           ("Source page", Elector.source_page), ("Raw extracted text", Elector.raw_text),
                           ("Parser version", Elector.parser_version),
                           ("Extraction method", Elector.extraction_method),
                           ("Extraction confidence", Elector.extraction_confidence)):
            prov.append((label, db.scalar(base.where(col.is_not(None))) or 0))
    return BoothDetail(station=st, ac=ac, district=district, state=state, row=row, roll=roll,
                       predecessors=predecessors, successors=successors,
                       ac_results=summarize_results(results), provenance_fields=prov)


# --- what changed ---------------------------------------------------------
@dataclass
class ChangeGroup:
    from_ac_number: int
    from_ac_name: str | None
    from_part_number: int
    from_station_id: int | None
    from_station_name: str | None
    from_agg: RecordAggregate | None
    mappings: list[MappingView]
    target_parts_2025: list[int]
    verified_stations: list[dict]
    electorate: Change

    @property
    def split(self) -> bool:
        return len(self.target_parts_2025) > 1


def ac_changes(db: Session, ac_id: int) -> tuple[AssemblyConstituency, District, list[ChangeGroup], dict] | None:
    ac = db.get(AssemblyConstituency, ac_id)
    if ac is None:
        return None
    district = db.get(District, ac.district_id)
    maps = evaluate_mappings(db, district.state_id)
    if ac.delimitation == DELIM_CURRENT:
        relevant = [m for m in maps if m.to_ac_number == ac.ac_number]
    else:
        relevant = [m for m in maps if m.from_ac_number == ac.ac_number
                    and m.from_edition == EDITION_2003]
    groups: dict[tuple[int, int], list[MappingView]] = {}
    for m in relevant:
        groups.setdefault((m.from_ac_number, m.from_part_number), []).append(m)
    sids = [ms[0].from_station_id for ms in groups.values() if ms[0].from_station_id]
    aggs = record_aggregates(db, sids) if sids else {}
    out = []
    for (acn, part), ms in sorted(groups.items()):
        sid = ms[0].from_station_id
        old = db.get(PollingStation, sid) if sid else None
        agg = aggs.get(sid)
        verified = {}
        for m in ms:
            if m.linked_station_id:
                verified.setdefault(m.linked_station_id, {
                    "station_id": m.linked_station_id, "part_number": m.to_part_number,
                    "name": m.linked_station_name, "villages": []})["villages"].append(m.area_name)
        out.append(ChangeGroup(
            from_ac_number=acn, from_ac_name=ms[0].from_ac_name, from_part_number=part,
            from_station_id=sid, from_station_name=station_label(old) if old else None,
            from_agg=agg, mappings=ms,
            target_parts_2025=sorted({m.to_part_number for m in ms if m.to_part_number is not None}),
            verified_stations=list(verified.values()),
            # current-roll electorate is not in the POC, so the change is unavailable
            electorate=compute_change(agg.total if agg else None, None)))
    ac_pairs = sorted({(m.from_ac_number, m.from_ac_name, m.to_ac_number, m.to_ac_name)
                       for m in relevant}, key=lambda x: (x[0] or 0, x[2] or 0))
    summary = {
        "mapping_rows": len(relevant),
        "verified": sum(1 for m in relevant if mapping_status_label(m.status) == "Verified"),
        "review": sum(1 for m in relevant if mapping_status_label(m.status) == "Review Required"),
        "parts_2003": len(groups),
        "split_parts": sum(1 for g in out if g.split),
        "renumbered": sum(1 for m in relevant if m.status == "review"
                          and m.same_number_station_id is not None),
        "with_suggestion": sum(1 for m in relevant if m.status == "review" and m.suggestions),
        "ac_pairs": ac_pairs,
        "electors_2003": sum(g.from_agg.total for g in out if g.from_agg),
    }
    return ac, district, out, summary


# --- data quality ---------------------------------------------------------
def _plural(n: int, noun: str) -> str:
    return f"{n:,} {noun}{'' if n == 1 else 's'}"


@dataclass
class Alert:
    level: str
    scope: str
    title: str
    detail: str
    href: str | None = None


def quality_overview(db: Session, backend: str, state_id: int | None = None,
                     include_results: bool = True) -> dict:
    """`include_results=False` skips the full per-row result views (the page shows only counts)."""
    sid = scope_state_id(db, state_id)
    q = quality_report(db, backend, state_id=sid)
    # a subquery, not an id list: a state can have more stations than SQLite allows parameters
    station_ids = select(PollingStation.id).where(PollingStation.ac_id.in_(_state_ac_ids(db, sid)))
    total = RecordAggregate()
    for a in record_aggregates(db, station_ids).values():
        total.add(a)
    rolls = db.scalars(select(ElectoralRoll).where(ElectoralRoll.extraction_status == "extracted",
                                                   ElectoralRoll.polling_station_id.in_(station_ids))
                       .order_by(ElectoralRoll.id)).all()
    roll_rows, alerts = [], []
    for r in rolls:
        s = booth_stats(db, r)
        st = db.get(PollingStation, r.polling_station_id)
        roll_rows.append({"roll": r, "stats": s, "station": st,
                          "diff": (r.extracted_elector_count or 0) - (r.official_elector_count or 0)})
        scope = f"AC {s.ac_number} · Part {s.part_number}"
        href = f"/station/{st.id}"
        clean = True
        if s.duplicate_rows:
            clean = False
            alerts.append(Alert("warn", scope, _plural(s.duplicate_rows, "duplicate record"),
                                "Same person or serial printed more than once in the published roll.", href))
        other = s.invalid_rows - s.duplicate_rows
        if other > 0:
            clean = False
            alerts.append(Alert("warn", scope, _plural(other, "other validation flag"),
                                f"Failed a rule other than duplication (EPIC format / gender / field "
                                f"rules); {_plural(s.invalid_rows, 'flag')} in total.", href))
        if s.low_confidence_rows:
            level = "warn" if s.low_confidence_rows / max(s.total_electors, 1) > LOW_CONF_BOOTH_SHARE else "info"
            alerts.append(Alert(level, scope, f"{s.low_confidence_rows:,} low-confidence rows",
                                "At least one legacy-font character could not be decoded; "
                                "scored honestly rather than guessed.", href))
        if s.serial_outliers:
            clean = False
            alerts.append(Alert("warn", scope, f"Stray serial {s.serial_outliers}",
                                "Printed in the source document; excluded from the derived count basis.", href))
        if s.serial_gaps:
            alerts.append(Alert("info", scope, f"{s.serial_gaps} serial gaps",
                                "Serials not printed in the source roll — normally deleted electors.", href))
        if clean:
            alerts.append(Alert("good", scope, "Clean extraction",
                                f"{s.total_electors:,} records, 0 validation flags, 0 duplicates, "
                                f"mean confidence {s.mean_confidence * 100:.2f}%.", href))

    mapping = mapping_summary_all(db, sid)
    if mapping["Review Required"]:
        alerts.append(Alert("warn", "Booth mapping",
                            f"{mapping['Review Required']} of {sum(mapping.values())} mapping relationships "
                            "need review",
                            "Historical and current part numbers are not stable, so these relationships are "
                            "not used as links until official evidence confirms them.", None))
    checks = _cached_checks(db, sid)
    f20 = form20_quality_summary(checks)
    current_ids = dict(db.execute(select(AssemblyConstituency.ac_number, AssemblyConstituency.id)
                                  .where(AssemblyConstituency.delimitation == DELIM_CURRENT,
                                         AssemblyConstituency.id.in_(_state_ac_ids(db, sid)))).all())
    for (year, acn), parts in f20["review_by_ac"].items():
        shown = ", ".join(map(str, parts[:10])) + (f" and {len(parts) - 10} more" if len(parts) > 10 else "")
        alerts.append(Alert("bad", f"Form 20 {year} · AC {acn}",
                            f"{_plural(len(parts), 'result row')} {'requires' if len(parts) == 1 else 'require'} review",
                            f"Part {shown}: the extracted vote columns do not reconcile with the printed "
                            "result sheet. Margin and candidate interpretation withheld.",
                            f"/ac/{current_ids[acn]}/performance" if acn in current_ids else None))
    if any(not r.named for r in checks):
        alerts.append(Alert("info", "Form 20", "Candidate names not attributed",
                            "Candidate names are rotated column headers that cannot be matched to vote "
                            "columns reliably; only unnamed leading/second vote counts are used.", None))
    if any(r.reliability == "not_backfilled" for r in checks):
        alerts.append(Alert("info", "Form 20", "Vote columns not backfilled",
                            "Run `python -m app backfill-form20` to enable booth margins.", None))

    fetch_sources = [row for row in db.execute(select(SourceFetch.source, func.count(),
                                                      _cnt(SourceFetch.ok.is_(True)))
                                               .group_by(SourceFetch.source)).all()
                     if _source_in_state(row[0], _scope_state_name(db, sid))]
    order = {"bad": 0, "warn": 1, "info": 2, "good": 3}
    alerts.sort(key=lambda a: order[a.level])
    return {"q": q, "totals": total, "rolls": roll_rows, "alerts": alerts,
            "mapping": mapping, "results": all_result_views(db, sid) if include_results else [], "form20": f20,
            "fetch_sources": fetch_sources, "stations_with_roll": sum(1 for _ in rolls)}


# --- home / navigation / search ------------------------------------------
def _source_in_state(source: str | None, state_name: str | None) -> bool:
    """A fetch source shown for a state: the shared ECI gateway, or the state's own sources."""
    sp = spec(state_name)
    return not (source or "").startswith("ceo_") or bool(
        sp and any((source or "").startswith(p) for p in sp.source_prefixes))


def default_ac_id(db: Session, state_id: int | None = None) -> int | None:
    """Navigation default within a state: the AC with official booth mapping, else the most
    results or stations. None when the state has no booth-level data."""
    sid = scope_state_id(db, state_id)
    in_state = AssemblyConstituency.id.in_(_state_ac_ids(db, sid))
    counts = Counter(m.to_ac_number for m in _state_mapping_rows(db, sid) if m.to_ac_number is not None)
    if counts:
        mapped = counts.most_common(1)[0][0]
        ac_id = db.scalar(select(AssemblyConstituency.id).where(
            in_state, AssemblyConstituency.ac_number == mapped,
            AssemblyConstituency.delimitation == DELIM_CURRENT))
        if ac_id:
            return ac_id
    has_booth_results = db.scalar(select(ElectionResult.id)
                                  .where(ElectionResult.ac_id.in_(_state_ac_ids(db, sid))).limit(1)) is not None
    if not has_booth_results:        # a state with constituency-level results only (e.g. Telangana)
        row = db.execute(select(AcResult.ac_id).where(AcResult.ac_id.in_(_state_ac_ids(db, sid)))
                         .order_by(AcResult.ac_number).limit(1)).first()
        if row:
            return row[0]
    row = db.execute(select(ElectionResult.ac_id, func.count())
                     .where(ElectionResult.ac_id.in_(_state_ac_ids(db, sid)))
                     .group_by(ElectionResult.ac_id).order_by(func.count().desc())).first()
    if row:
        return row[0]
    row = db.execute(select(PollingStation.ac_id, func.count().label("n"))
                     .join(AssemblyConstituency, PollingStation.ac_id == AssemblyConstituency.id)
                     .where(AssemblyConstituency.delimitation == DELIM_CURRENT, in_state)
                     .group_by(PollingStation.ac_id).order_by(func.count().desc())).first()
    return row[0] if row else None


# --- data availability (what exists for each AC) ---------------------------
@dataclass
class AcAvailability:
    ac: AssemblyConstituency
    district: District
    current_stations: int = 0
    historical_stations: int = 0
    current_roll_records: int = 0
    historical_roll_records: int = 0
    historical_parts_with_roll: int = 0
    roll_years: list[int] = field(default_factory=list)
    result_records: int = 0
    result_years: list[int] = field(default_factory=list)
    ac_result_years: list[int] = field(default_factory=list)    # constituency-level (ECI report)
    mapping_verified: int = 0
    mapping_review: int = 0
    mapping_unmapped: int = 0
    linked_acs: list[dict] = field(default_factory=list)   # other delimitation, via mapping

    @property
    def is_current(self) -> bool:
        return self.ac.delimitation == DELIM_CURRENT

    @property
    def mapping_status(self) -> str:
        if not (self.mapping_verified or self.mapping_review):
            return "none"
        return "verified" if not self.mapping_review else "partial"

    def matrix(self) -> list[dict]:
        """Rows of a data-availability table: state is yes | no | partial."""
        years = ", ".join(map(str, self.result_years))
        linked = ", ".join(f"AC {a['ac_number']} {a['name']} ({a['delimitation']})"
                           for a in self.linked_acs)
        relationships = (f"{self.mapping_verified + self.mapping_review + self.mapping_unmapped} village-level "
                         f"relationships — {self.mapping_verified} verified · {self.mapping_review} review "
                         f"required · {self.mapping_unmapped} not mapped")
        mapping = {
            "verified": ("yes", relationships),
            "partial": ("partial", relationships),
            "none": ("no", "No official mapping ingested"),
        }[self.mapping_status]
        results = (("yes", f"{years} Form 20 — {self.result_records:,} booth records")
                   if self.result_records else ("no", "Not available in current POC"))
        if self.is_current:
            return [
                {"label": "Current polling stations",
                 **({"state": "yes", "detail": f"{self.current_stations:,} stations (SIR-2026)"}
                    if self.current_stations else {"state": "no", "detail": "Not yet ingested"})},
                {"label": "Current electoral roll",
                 **({"state": "yes", "detail": f"{self.current_roll_records:,} records"}
                    if self.current_roll_records else
                    {"state": "no", "detail": "Not available from the current public source"})},
                {"label": "Historical electoral roll", "state": "no",
                 "detail": f"Held under {linked}" if linked else "Not available in current POC"},
                {"label": "Historical results", "state": results[0], "detail": results[1]},
                {"label": "Booth mapping", "state": mapping[0], "detail": mapping[1]},
            ]
        return [
            {"label": "Historical electoral roll",
             **({"state": "yes", "detail": f"{', '.join(map(str, self.roll_years))} roll — "
                                          f"{self.historical_roll_records:,} records, "
                                          f"{self.historical_parts_with_roll} parts"}
                if self.historical_roll_records else {"state": "no", "detail": "Not ingested"})},
            {"label": "Historical results", "state": results[0],
             "detail": results[1] if self.result_records else
             f"No result ingested for the {self.ac.delimitation} delimitation"},
            {"label": "Booth mapping", "state": mapping[0], "detail": mapping[1]},
            {"label": "Current constituency", "state": "partial" if linked else "no",
             "detail": f"Linked by official mapping to {linked}" if linked
             else "Historical constituency — no current equivalent linked"},
        ]


def all_availability(db: Session, ac_ids: list[int] | None = None,
                     state_id: int | None = None) -> list[AcAvailability]:
    """Availability for the given ACs, or for every AC of one state (default state)."""
    stmt = (select(AssemblyConstituency, District)
            .join(District, AssemblyConstituency.district_id == District.id)
            .order_by(District.district_name, AssemblyConstituency.delimitation.desc(),
                      AssemblyConstituency.ac_number))
    if ac_ids is not None:
        stmt = stmt.where(AssemblyConstituency.id.in_(ac_ids))
    else:
        stmt = stmt.where(District.state_id == scope_state_id(db, state_id))
    avs = {a.id: AcAvailability(a, d) for a, d in db.execute(stmt)}
    if not avs:
        return []

    for ac_id, ed, n in db.execute(select(PollingStation.ac_id, PollingStation.edition, func.count())
                                   .group_by(PollingStation.ac_id, PollingStation.edition)):
        if ac_id in avs:
            if ed == EDITION_CURRENT:
                avs[ac_id].current_stations += n
            else:
                avs[ac_id].historical_stations += n
    for ac_id, ed, n in db.execute(
            select(PollingStation.ac_id, PollingStation.edition, func.count(Elector.id))
            .join(Elector, Elector.polling_station_id == PollingStation.id)
            .group_by(PollingStation.ac_id, PollingStation.edition)):
        if ac_id in avs:
            if ed == EDITION_CURRENT:
                avs[ac_id].current_roll_records += n
            else:
                avs[ac_id].historical_roll_records += n
    for ac_id, year, n in db.execute(
            select(PollingStation.ac_id, ElectoralRoll.roll_year, func.count(ElectoralRoll.id))
            .join(ElectoralRoll, ElectoralRoll.polling_station_id == PollingStation.id)
            .where(PollingStation.edition != EDITION_CURRENT,
                   ElectoralRoll.extraction_status == "extracted")
            .group_by(PollingStation.ac_id, ElectoralRoll.roll_year)):
        if ac_id in avs:
            avs[ac_id].historical_parts_with_roll += n
            avs[ac_id].roll_years = sorted(set(avs[ac_id].roll_years) | {year})

    # results: by ac_id, or by number for rows not yet linked (current delimitation of the
    # election's own state only — every state has an AC with the same number)
    current_by_number = {(st_name, n): i for n, i, st_name in db.execute(
        select(AssemblyConstituency.ac_number, AssemblyConstituency.id, State.state_name)
        .join(District, AssemblyConstituency.district_id == District.id)
        .join(State, District.state_id == State.id)
        .where(AssemblyConstituency.delimitation == DELIM_CURRENT))}
    for ac_id, ac_number, st_name, year, n in db.execute(
            select(ElectionResult.ac_id, ElectionResult.ac_number, Election.state,
                   Election.election_year, func.count())
            .join(Election, ElectionResult.election_id == Election.id)
            .group_by(ElectionResult.ac_id, ElectionResult.ac_number, Election.state,
                      Election.election_year)):
        target = ac_id if ac_id is not None else current_by_number.get((st_name, ac_number))
        if target in avs:
            avs[target].result_records += n
            avs[target].result_years = sorted(set(avs[target].result_years) | {year})
    for ac_id, year in db.execute(select(AcResult.ac_id, Election.election_year)
                                  .join(Election, AcResult.election_id == Election.id)
                                  .where(AcResult.ac_id.is_not(None)).distinct()):
        if ac_id in avs:
            avs[ac_id].ac_result_years = sorted(set(avs[ac_id].ac_result_years) | {year})

    maps_by_state: dict[int, list[MappingView]] = {}
    keys_by_state: dict[int, dict] = {}
    for av in avs.values():
        sid = av.district.state_id
        if sid not in maps_by_state:
            maps_by_state[sid] = evaluate_mappings(db, sid)
            keys_by_state[sid] = {(a.ac_number, a.delimitation): a.id for a in db.scalars(
                select(AssemblyConstituency).where(AssemblyConstituency.id.in_(_state_ac_ids(db, sid))))}
        maps, acs_by_key = maps_by_state[sid], keys_by_state[sid]
        if av.is_current:
            rel = [m for m in maps if m.to_ac_number == av.ac.ac_number]
            linked = {(m.from_ac_number, m.from_ac_name, "2003") for m in rel}
        else:
            rel = [m for m in maps if m.from_edition == EDITION_2003
                   and m.from_ac_number == av.ac.ac_number]
            linked = {(m.to_ac_number, m.to_ac_name, DELIM_CURRENT) for m in rel}
        av.mapping_verified = sum(1 for m in rel if mapping_status_label(m.status) == "Verified")
        av.mapping_review = sum(1 for m in rel if mapping_status_label(m.status) == "Review Required")
        av.mapping_unmapped = sum(1 for m in rel if mapping_status_label(m.status) == "Not Mapped")
        av.linked_acs = [{"ac_number": n, "name": name, "delimitation": dl,
                          "id": acs_by_key.get((n, dl))}
                         for n, name, dl in sorted(linked, key=lambda x: x[0] or 0)]
    return list(avs.values())


def state_summary(db: Session, state_id: int | None = None) -> dict:
    sid = scope_state_id(db, state_id)
    state = db.get(State, sid) if sid else None
    sp = spec(state.state_name if state else None)
    avs = all_availability(db, state_id=sid)
    districts = db.scalars(select(District).where(District.state_id == sid)
                           .order_by(District.district_name)).all()
    rows = []
    for d in districts:
        mine = [a for a in avs if a.district.id == d.id]
        cur = [a for a in mine if a.is_current]
        rows.append({
            "district": d, "acs": len(cur), "historical_acs": len(mine) - len(cur),
            "stations": sum(a.current_stations for a in mine),
            "historical_stations": sum(a.historical_stations for a in mine),
            "has_results": any(a.result_records for a in mine),
            "has_ac_results": any(a.ac_result_years for a in mine),
            "has_historical_roll": any(a.historical_roll_records for a in mine),
            "has_mapping": any(a.mapping_status != "none" for a in mine),
        })
    cur = [a for a in avs if a.is_current]
    return {
        "districts": len(districts), "acs_current": len(cur),
        "acs_historical": len(avs) - len(cur),
        "stations_current": sum(a.current_stations for a in avs),
        "acs_with_stations": sum(1 for a in cur if a.current_stations),
        "electors": sum(a.historical_roll_records + a.current_roll_records for a in avs),
        "mappings": len(_state_mapping_rows(db, sid)),
        "results": sum(a.result_records for a in avs),
        "acs_with_ac_results": sum(1 for a in avs if a.ac_result_years),
        "ac_result_years": sorted({y for a in avs for y in a.ac_result_years}),
        "district_rows": rows, "availability": avs,
        "state": state, "booth_sources": bool(sp and sp.booth_sources),
        "result_sources": bool(sp and sp.result_sources),
        "form20_years": list(sp.form20_years) if sp else [],
    }


def state_ac_results_quality(db: Session, state_id: int | None = None) -> dict:
    """Constituency-level (ECI report) counts for a state: candidate rows, constituency
    results (one per AC and year) and how many pass `ac_result_check` — the same check
    as the constituency pages."""
    name = _scope_state_name(db, state_id)
    groups: dict[tuple[int, int], list[AcResult]] = {}
    for r, year in db.execute(select(AcResult, Election.election_year)
                              .join(Election, AcResult.election_id == Election.id)
                              .where(Election.state == name)):
        groups.setdefault((year, r.ac_number), []).append(r)
    return {"rows": sum(len(g) for g in groups.values()), "results": len(groups),
            "verified": sum(1 for g in groups.values() if ac_result_check(g)[0])}


def state_results_quality(db: Session, state_id: int | None = None) -> dict:
    """Form 20 verified/review counts for a state — the same shared calculation as every other page."""
    return form20_quality_summary(_cached_checks(db, state_id))


_CHECKS_CACHE: dict[tuple, list] = {}


def _cached_checks(db: Session, state_id: int | None) -> list[ResultCheck]:
    """`result_checks`, reused while the state's rows are unchanged. Results are only ever
    replaced by delete + insert, so the (row count, highest id) pair changes with any write."""
    name = _scope_state_name(db, state_id)
    key = (str(db.get_bind().url), name) + tuple(db.execute(
        select(func.count(ElectionResult.id), func.max(ElectionResult.id))
        .join(Election, ElectionResult.election_id == Election.id)
        .where(Election.state == name)).one())
    if key not in _CHECKS_CACHE:
        for stale in [k for k in _CHECKS_CACHE if k[:2] == key[:2]]:   # one entry per state
            del _CHECKS_CACHE[stale]
        _CHECKS_CACHE[key] = result_checks(db, state_id)
    return _CHECKS_CACHE[key]


def ac_observations(av: AcAvailability, summaries: list[ElectionSummary],
                    roll_stats: list | None = None) -> list[str]:
    """At most four plain-language facts, each computed from stored data."""
    obs: list[str] = []
    if not av.is_current and roll_stats:
        years = ", ".join(map(str, av.roll_years))
        obs.append(f"{av.historical_roll_records:,} elector records were extracted from "
                   f"{len(roll_stats)} parts of the {years} electoral roll.")
        clean = sum(1 for s in roll_stats if not s.invalid_rows and not s.serial_outliers)
        obs.append(f"{clean} of {len(roll_stats)} parts are clean; {len(roll_stats) - clean} "
                   "require review for duplicates or validation flags.")
        dups = sum(s.duplicate_rows for s in roll_stats)
        if dups:
            obs.append(f"{_plural(dups, 'duplicate record')} were found in the published roll "
                       "and kept for review, not deleted.")
    for e in summaries[:1]:
        obs.append(f"{e.booths:,} polling-station result records are available for the "
                   f"{e.year} election.")
        if e.zero_margin:
            obs.append(f"{_plural(e.zero_margin, 'historical booth')} recorded a zero vote margin "
                       "(the top two vote columns are tied).")
        elif e.narrowest:
            obs.append(f"The narrowest historical booth margin is {e.narrowest.margin.margin:,} "
                       f"votes (Part {e.narrowest.part_number}).")
        obs.append(f"{e.sum_check_pass:,} of {e.booths:,} result rows pass the vote-column "
                   "consistency check.")
    if av.is_current and not av.current_roll_records:
        obs.append("Current 2026 voter-roll data is not available from the current public source.")
    if not av.is_current and not av.result_records:
        obs.append(f"No election result is ingested for the {av.ac.delimitation} delimitation.")
    if av.is_current and not av.current_stations and not av.result_records:
        obs.insert(0, "Only this constituency's official identity (number, name, district) is "
                      "stored; booth-level data has not been ingested yet.")
    return obs[:4]


# --- booth mapping table ----------------------------------------------------
@dataclass
class MappingRow:
    hist_ac_number: int
    hist_ac_name: str | None
    hist_part: int
    hist_station_id: int | None
    hist_area: str | None
    current_part: str | None
    current_station_id: int | None
    current_area: str | None
    status: str          # Verified | Review Required | Not Mapped
    note: str


MAPPING_STATUSES = ("Verified", "Review Required", "Not Mapped")


def mapping_status_label(status: str | None) -> str:
    """The display status of a mapping relationship. Only an explicit `verified` is Verified and
    only `unmapped` is Not Mapped; `review`, NULL or any unknown value is Review Required."""
    return {"verified": "Verified", "unmapped": "Not Mapped"}.get(status or "", "Review Required")


def mapping_summary(rows: list["MappingRow"]) -> dict[str, int]:
    """Counts per status for mapping-table rows — one row per unique mapping relationship,
    so the summary always equals the table."""
    counts = {k: 0 for k in MAPPING_STATUSES}
    for r in rows:
        counts[r.status] += 1
    return counts


def mapping_summary_all(db: Session, state_id: int | None = None) -> dict[str, int]:
    """Mapping counts over every current constituency of a state that has mapping relationships."""
    sid = scope_state_id(db, state_id)
    total = {k: 0 for k in MAPPING_STATUSES}
    numbers = {m.to_ac_number for m in _state_mapping_rows(db, sid) if m.to_ac_number is not None}
    for ac_id in db.scalars(select(AssemblyConstituency.id).where(
            AssemblyConstituency.id.in_(_state_ac_ids(db, sid)),
            AssemblyConstituency.delimitation == DELIM_CURRENT,
            AssemblyConstituency.ac_number.in_(numbers))):
        for k, v in mapping_table(db, ac_id)[3].items():
            total[k] += v
    return total


def mapping_table(db: Session, ac_id: int):
    ac = db.get(AssemblyConstituency, ac_id)
    if ac is None:
        return None
    district = db.get(District, ac.district_id)
    maps = evaluate_mappings(db, district.state_id)
    if ac.delimitation == DELIM_CURRENT:
        rel = [m for m in maps if m.to_ac_number == ac.ac_number]
        hist_numbers = {m.from_ac_number for m in rel}
    else:
        rel = [m for m in maps if m.from_edition == EDITION_2003 and m.from_ac_number == ac.ac_number]
        hist_numbers = {ac.ac_number}

    rows: list[MappingRow] = []
    for m in rel:
        base = dict(hist_ac_number=m.from_ac_number, hist_ac_name=m.from_ac_name,
                    hist_part=m.from_part_number, hist_station_id=m.from_station_id,
                    hist_area=m.area_name)
        label = mapping_status_label(m.status)
        if label == "Verified" and m.linked_station_id is None:
            label = "Review Required"            # a Verified row must name its current station
        if label == "Not Mapped":
            rows.append(MappingRow(**base, current_part=None, current_station_id=None,
                                   current_area=None, status="Not Mapped", note=m.reason))
        elif label == "Verified":
            rows.append(MappingRow(**base, current_part=f"Part {m.linked_part_number}",
                                   current_station_id=m.linked_station_id,
                                   current_area=m.linked_station_name, status="Verified",
                                   note=m.reason))
        elif m.suggestions:
            one = m.suggestions[0] if len(m.suggestions) == 1 else None
            rows.append(MappingRow(
                **base, current_part="Possible: Part " + ", ".join(str(s["part_number"]) for s in m.suggestions),
                current_station_id=one["station_id"] if one else None,
                current_area=one["name"] if one else None, status="Review Required",
                note=f"2025 roll: Part {m.to_part_number}. {m.reason}"))
        else:
            rows.append(MappingRow(**base, current_part=None, current_station_id=None,
                                   current_area=None, status="Review Required",
                                   note=f"2025 roll: Part {m.to_part_number}. {m.reason}"))

    mapped = {(m.from_ac_number, m.from_part_number) for m in rel}
    if hist_numbers:
        for st, acn, acname in db.execute(
                select(PollingStation, AssemblyConstituency.ac_number, AssemblyConstituency.ac_name)
                .join(AssemblyConstituency, PollingStation.ac_id == AssemblyConstituency.id)
                .where(AssemblyConstituency.delimitation != DELIM_CURRENT,
                       AssemblyConstituency.id.in_(_state_ac_ids(db, district.state_id)),
                       AssemblyConstituency.ac_number.in_(hist_numbers),
                       PollingStation.edition == EDITION_2003)):
            if (acn, st.part_number) not in mapped:
                rows.append(MappingRow(acn, acname, st.part_number, st.id, st.area_description,
                                       None, None, None, "Not Mapped",
                                       "No official village mapping ingested for this part."))
    order = {"Verified": 0, "Review Required": 1, "Not Mapped": 2}
    rows.sort(key=lambda r: (r.hist_ac_number, r.hist_part, order[r.status]))
    return ac, district, rows, mapping_summary(rows)


@dataclass
class SearchHit:
    kind: str
    title: str
    subtitle: str
    href: str
    detail: str = ""


_PART_QUERY = re.compile(r"(?:part|booth)\s*(?:no\.?\s*)?(\d{1,4})", re.I)


def search(db: Session, q: str, limit: int = 60, state_id: int | None = None) -> list[SearchHit]:
    """Constituencies, polling stations and historical results matching a name or number.

    "Part 141" / "booth 10" search part numbers only. Hits for the featured constituency
    (the one with booth mapping) and its linked historical constituency are listed first.
    """
    q = (q or "").strip()
    if not q:
        return []
    part_q = _PART_QUERY.fullmatch(q)
    number = int(part_q.group(1)) if part_q else (int(q) if q.isdigit() else None)
    like = f"%{q}%"
    sid = scope_state_id(db, state_id)
    ac_rows = {a.id: (a, d) for a, d in db.execute(
        select(AssemblyConstituency, District)
        .join(District, AssemblyConstituency.district_id == District.id)
        .where(District.state_id == sid))}
    featured_ids: set[int] = set()
    featured = default_ac_id(db, sid)
    if featured in ac_rows:
        fa = ac_rows[featured][0]
        linked = {m.from_ac_number for m in _state_mapping_rows(db, sid) if m.to_ac_number == fa.ac_number}
        featured_ids = {featured} | {i for i, (a, _) in ac_rows.items()
                                     if a.delimitation != DELIM_CURRENT and a.ac_number in linked}
    ranked: list[tuple[int, SearchHit]] = []

    def add(hit: SearchHit, ac_id: int | None) -> None:
        ranked.append((0 if ac_id in featured_ids else 1, hit))

    if not part_q:
        dist_filter = or_(District.district_name.ilike(like), District.district_name_local.ilike(like))
        for d in db.scalars(select(District).where(dist_filter, District.state_id == sid)
                            .order_by(District.district_name)):
            add(SearchHit("District", d.district_name, d.district_name_local or "", f"/district/{d.id}", ""), None)
        ac_filter = [AssemblyConstituency.ac_name.ilike(like), AssemblyConstituency.ac_name_local.ilike(like)]
        if number is not None:
            ac_filter.append(AssemblyConstituency.ac_number == number)
        for a in db.scalars(select(AssemblyConstituency).where(or_(*ac_filter),
                                                               AssemblyConstituency.id.in_(list(ac_rows)))
                            .order_by(AssemblyConstituency.delimitation.desc(), AssemblyConstituency.ac_number)):
            d = ac_rows[a.id][1]
            label = f"AC {a.ac_number} — {a.ac_name}" if a.delimitation == DELIM_CURRENT \
                else f"Historical AC {a.ac_number} — {a.ac_name}"
            add(SearchHit("Constituency", label,
                          f"{d.district_name} · {'current constituency' if a.delimitation == DELIM_CURRENT else a.delimitation + ' delimitation'}",
                          f"/ac/{a.id}"), a.id)

    if part_q:
        st_filter = [PollingStation.part_number == number]
    else:
        st_filter = [PollingStation.polling_station_name.ilike(like),
                     PollingStation.polling_station_name_local.ilike(like),
                     PollingStation.area_description.ilike(like)]
        if number is not None:
            st_filter.append(PollingStation.part_number == number)
    stations = db.scalars(select(PollingStation).where(or_(*st_filter),
                                                       PollingStation.ac_id.in_(list(ac_rows)))
                          .order_by(PollingStation.edition, PollingStation.part_number)).all()
    aggs = record_aggregates(db, [s.id for s in stations]) if stations else {}
    for s in stations:
        a, d = ac_rows[s.ac_id]
        agg = aggs.get(s.id)
        current = s.edition == EDITION_CURRENT
        add(SearchHit(
            "Current polling station" if current else "Historical roll part",
            f"Part {s.part_number} — {s.polling_station_name or station_label(s)}",
            f"AC {a.ac_number} {a.ac_name} · {d.district_name} · "
            + ("current 2026" if current else "historical electoral roll 2003"),
            f"/station/{s.id}",
            f"{agg.total:,} electors (2003 roll)" if agg
            else "Current voter roll: not available from the current public source"), s.ac_id)

    if number is not None:
        current_by_number = {a.ac_number: i for i, (a, _) in ac_rows.items() if a.delimitation == DELIM_CURRENT}
        for r, e in db.execute(select(ElectionResult, Election)
                               .join(Election, ElectionResult.election_id == Election.id)
                               .where(ElectionResult.part_number == number,
                                      Election.state == _scope_state_name(db, sid))
                               .order_by(Election.election_year, ElectionResult.ac_number)):
            ac_id = r.ac_id or current_by_number.get(r.ac_number)
            a, d = ac_rows.get(ac_id, (None, None))
            add(SearchHit(
                "Historical result",
                f"Part {r.part_number} — {r.polling_station_name or 'polling station'}",
                f"AC {r.ac_number} {a.ac_name if a else ''} · {d.district_name if d else ''} · "
                f"historical result {e.election_year} ({e.election_year} numbering)",
                f"/result/{r.id}",
                f"{r.total_votes:,} total votes" if r.total_votes is not None else ""), ac_id)

    ranked.sort(key=lambda x: x[0])
    return [h for _, h in ranked][:limit]


AGE_BAND_NAMES = [b[0] for b in AGE_BANDS]
