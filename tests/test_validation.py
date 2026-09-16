"""Validation, normalisation and duplicate-detection tests."""
from dataclasses import dataclass

from app.services.validation import (
    find_serial_outliers, looks_like_epic, normalize_age, normalize_gender,
    normalize_name, validate_rows,
)


@dataclass
class Row:
    serial_number: int | None = 1
    elector_name: str | None = "राम"
    relative_name: str | None = "श्याम"
    relative_type: str | None = "FATHER"
    age: int | None = 40
    gender: str | None = "M"
    epic_number: str | None = None
    part_number: int | None = None
    is_valid: bool = True
    validation_notes: str | None = None
    duplicate_kind: str | None = None


class TestNormalisation:
    def test_gender_maps_to_the_four_allowed_values(self):
        assert normalize_gender("M") == "M"
        assert normalize_gender("पुरुष") == "M"
        assert normalize_gender("महिला") == "F"
        assert normalize_gender("अन्य") == "O"
        assert normalize_gender(None) == "UNKNOWN"
        assert normalize_gender("???") == "UNKNOWN"

    def test_age_outside_the_plausible_range_becomes_null_not_a_guess(self):
        assert normalize_age(35) == 35
        assert normalize_age("35") == 35
        assert normalize_age(17) is None      # below the franchise age
        assert normalize_age(250) is None
        assert normalize_age("") is None
        assert normalize_age(None) is None

    def test_name_folding_is_only_for_comparison(self):
        assert normalize_name("राम  कुमार") == normalize_name("रामकुमार")
        assert normalize_name(None) == ""

    def test_epic_formats(self):
        assert looks_like_epic("MYC0239293")            # modern
        assert looks_like_epic("UP/1/423/0153455")      # legacy UP-era
        assert looks_like_epic("UP\\1\\423\\0153455")
        assert not looks_like_epic("NULL")
        assert not looks_like_epic("123")
        assert not looks_like_epic(None)


class TestValidation:
    def test_clean_rows_pass(self):
        # Distinct people: identical name+relative+age would (correctly) be
        # reported as fuzzy duplicates.
        rep = validate_rows([Row(serial_number=i, elector_name=f"व्यक्ति{i}")
                             for i in range(1, 6)])
        assert rep.checked == 5
        assert rep.invalid == 0
        assert rep.validity_rate == 100.0

    def test_bad_age_is_flagged_not_dropped(self):
        rows = [Row(serial_number=1, age=5)]
        rep = validate_rows(rows)
        assert rep.invalid == 1
        assert rows[0].is_valid is False
        assert "age" in rows[0].validation_notes
        assert rep.checked == 1          # the row is still counted, never removed

    def test_duplicate_serial_is_linked_not_deleted(self):
        rows = [Row(serial_number=1), Row(serial_number=1, elector_name="सीता")]
        rep = validate_rows(rows)
        assert rep.duplicates_exact == 1
        assert rows[1].duplicate_kind == "same_part_and_serial"
        assert len(rows) == 2

    def test_same_person_signature_is_flagged_as_a_fuzzy_duplicate(self):
        rows = [Row(serial_number=1), Row(serial_number=2)]
        rep = validate_rows(rows)
        assert rep.duplicates_fuzzy == 1
        assert rows[1].duplicate_kind == "same_name_relative_age"

    def test_part_mismatch_against_the_parent_station_is_reported(self):
        rows = [Row(serial_number=1, part_number=9)]
        rep = validate_rows(rows, expected_part=6)
        assert rep.invalid == 1
        assert "does not match the parent station" in rows[0].validation_notes

    def test_serial_gaps_are_reported(self):
        rows = [Row(serial_number=n, elector_name=f"व्यक्ति{n}") for n in (1, 2, 4, 5)]
        rep = validate_rows(rows)
        assert rep.serial_gaps == [3]
        assert rep.max_serial == 5


class TestSerialOutliers:
    def test_a_stray_high_serial_is_excluded_from_the_expected_count(self):
        # Real case: AC15 part 10 prints serial 2220 on its last row after 1275.
        serials = list(range(1, 1276)) + [2220]
        assert find_serial_outliers(serials) == [2220]

    def test_a_contiguous_roll_has_no_outliers(self):
        assert find_serial_outliers(list(range(1, 343))) == []

    def test_ordinary_interior_gaps_are_not_outliers(self):
        # Deleted electors leave real gaps; those must not be explained away.
        serials = [n for n in range(1, 1073) if n not in {354, 355, 356, 357, 686}]
        assert find_serial_outliers(serials) == []

    def test_expected_count_ignores_the_stray_serial(self):
        rows = [Row(serial_number=n, elector_name=f"व्यक्ति{n}")
                for n in list(range(1, 1276)) + [2220]]
        rep = validate_rows(rows)
        assert rep.max_serial == 2220
        assert rep.expected_count == 1275
        assert "stray serial" in rep.expected_basis
        assert rep.serial_gaps == []
