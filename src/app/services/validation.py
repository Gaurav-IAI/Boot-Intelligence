"""Validation and duplicate detection for extracted elector records.

Nothing is deleted. Rows that fail a rule are marked `is_valid=False` with a note,
and duplicates are linked to the row they duplicate with a `duplicate_kind`, so a
human can review them. Silently dropping records would hide exactly the quality
problems this POC exists to measure.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

MIN_AGE = 18
MAX_AGE = 120
VALID_GENDERS = {"M", "F", "O", "UNKNOWN"}
VALID_RELATIONS = {"FATHER", "MOTHER", "HUSBAND", "OTHER"}

# EPIC formats seen in Uttarakhand rolls: modern "ABC1234567" and the legacy
# UP-era form "UP/1/423/0153455" (also written with backslashes).
EPIC_MODERN = re.compile(r"^[A-Z]{3}\d{7}$")
EPIC_LEGACY = re.compile(r"^[A-Z]{2}[\\/]\d+[\\/]\d+[\\/]\d+$")


@dataclass
class RowIssue:
    serial_number: int | None
    field_name: str
    problem: str


@dataclass
class ValidationReport:
    checked: int = 0
    valid: int = 0
    invalid: int = 0
    issues: list[RowIssue] = field(default_factory=list)
    duplicates_exact: int = 0
    duplicates_fuzzy: int = 0
    serial_gaps: list[int] = field(default_factory=list)
    max_serial: int | None = None
    serial_outliers: list[int] = field(default_factory=list)
    expected_count: int | None = None
    expected_basis: str = ""

    @property
    def validity_rate(self) -> float:
        return (self.valid / self.checked * 100.0) if self.checked else 0.0


def normalize_name(name: str | None) -> str:
    """Fold a name for duplicate comparison (NOT for storage)."""
    if not name:
        return ""
    s = unicodedata.normalize("NFC", name)
    s = re.sub(r"[\s​-‍ ]+", "", s)
    return s.casefold()


def normalize_gender(raw: str | None) -> str:
    if not raw:
        return "UNKNOWN"
    v = raw.strip().upper()
    if v in VALID_GENDERS:
        return v
    if v.startswith(("M", "पु")):
        return "M"
    if v.startswith(("F", "W", "मह", "स्त्")):
        return "F"
    if v.startswith(("O", "T", "अन")):
        return "O"
    return "UNKNOWN"


def normalize_age(raw: object) -> int | None:
    """Return the age only when it is plausible; otherwise None."""
    try:
        age = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return age if MIN_AGE <= age <= MAX_AGE else None


def looks_like_epic(value: str | None) -> bool:
    if not value:
        return False
    v = value.strip().upper()
    return bool(EPIC_MODERN.match(v) or EPIC_LEGACY.match(v))


OUTLIER_JUMP = 100


def find_serial_outliers(serials: list[int]) -> list[int]:
    """Serials that sit far beyond the end of the main run.

    Published rolls occasionally carry a mistyped serial (e.g. 2220 printed on
    the last row of a 1,276-row part). Treating that as the roll's length would
    fabricate a huge extraction shortfall, so such values are identified and
    excluded from the expected-count basis instead of being silently trusted.
    """
    if len(serials) < 10:
        return []
    ordered = sorted(serials)
    outliers: list[int] = []
    for i in range(len(ordered) - 1, 0, -1):
        if ordered[i] - ordered[i - 1] > OUTLIER_JUMP:
            # Only the sparse tail after a big jump counts as anomalous.
            tail = ordered[i:]
            if len(tail) <= max(3, len(ordered) // 100):
                outliers = tail
        else:
            break
    return outliers


def validate_rows(rows: list, expected_part: int | None = None) -> ValidationReport:
    """Validate parsed elector rows.

    `rows` are objects with the attributes produced by the roll parser. The
    report is advisory: callers decide what to persist, but every row is stored.
    """
    rep = ValidationReport()
    seen_exact: dict[tuple, int] = {}
    seen_fuzzy: dict[tuple, int] = {}
    serials: list[int] = []

    for r in rows:
        rep.checked += 1
        problems: list[str] = []

        if r.serial_number is None:
            problems.append("serial_number missing")
        else:
            serials.append(r.serial_number)

        if not r.elector_name:
            problems.append("elector_name missing")

        raw_age = getattr(r, "age", None)
        if raw_age is not None and normalize_age(raw_age) is None:
            problems.append(f"age {raw_age!r} outside {MIN_AGE}-{MAX_AGE}")

        g = normalize_gender(getattr(r, "gender", None))
        if g == "UNKNOWN":
            problems.append(f"gender {getattr(r, 'gender', None)!r} not recognised")

        rel = getattr(r, "relative_type", None)
        if rel is not None and rel not in VALID_RELATIONS:
            problems.append(f"relative_type {rel!r} not recognised")

        epic = getattr(r, "epic_number", None)
        if epic and not looks_like_epic(epic):
            problems.append(f"epic {epic!r} does not match a known EPIC format")

        if expected_part is not None:
            part = getattr(r, "part_number", None)
            if part is not None and part != expected_part:
                problems.append(f"part {part} does not match the parent station {expected_part}")

        # Duplicates: same serial is exact; same person signature is fuzzy.
        if r.serial_number is not None:
            key = (r.serial_number,)
            if key in seen_exact:
                rep.duplicates_exact += 1
                r.duplicate_of_serial = seen_exact[key]
                r.duplicate_kind = "same_part_and_serial"
                problems.append(f"duplicate serial (first seen at row {seen_exact[key]})")
            else:
                seen_exact[key] = r.serial_number

        fkey = (normalize_name(r.elector_name),
                normalize_name(getattr(r, "relative_name", None)),
                normalize_age(raw_age))
        if all(fkey) and fkey in seen_fuzzy:
            rep.duplicates_fuzzy += 1
            r.duplicate_of_serial = seen_fuzzy[fkey]
            r.duplicate_kind = "same_name_relative_age"
            problems.append(f"same name+relative+age as serial {seen_fuzzy[fkey]}")
        elif all(fkey):
            seen_fuzzy[fkey] = r.serial_number

        if problems:
            rep.invalid += 1
            for p in problems:
                rep.issues.append(RowIssue(r.serial_number, "row", p))
            r.validation_notes = "; ".join(problems)
            r.is_valid = False
        else:
            rep.valid += 1
            r.validation_notes = None
            r.is_valid = True

    if serials:
        rep.max_serial = max(serials)
        present = set(serials)
        rep.serial_gaps = [n for n in range(1, rep.max_serial + 1) if n not in present]
        rep.serial_outliers = find_serial_outliers(serials)
        # Deriving the expected count from the highest printed serial is only
        # safe once stray serials are excluded: real Uttarakhand rolls contain
        # data-entry anomalies (AC15 part 10 ends with serial 2220 after 1275),
        # which would otherwise invent ~900 phantom "missing" electors.
        usable = [s for s in serials if s not in set(rep.serial_outliers)]
        if usable:
            rep.expected_count = max(usable)
            rep.expected_basis = (
                "highest serial number printed in the roll"
                if not rep.serial_outliers else
                f"highest serial number printed, excluding {len(rep.serial_outliers)} "
                f"stray serial(s) {rep.serial_outliers[:3]} present in the source"
            )
            rep.serial_gaps = [n for n in range(1, rep.expected_count + 1)
                               if n not in present]
    return rep
