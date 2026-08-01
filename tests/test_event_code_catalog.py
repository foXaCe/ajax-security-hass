"""Tests for the Ajax event-code catalogue and its display fallback.

``EVENT_CODES`` maps the codes the integration reacts on (action key, category,
``is_alarm``). ``EVENT_CODE_DESCRIPTIONS`` covers the rest of Ajax's published
catalogue and is display-only: an unmapped code shows its official English
label instead of a raw ``M_09_26``, without gaining any alarm semantics.

These tests pin that contract and guard the generated table against the
formatting problems present in Ajax's source file (leftover ``${SOURCE}``
placeholders, French rows).
"""

from __future__ import annotations

import re

from custom_components.ajax.event_codes import (
    DEVICE_TYPES,
    EVENT_CODE_DESCRIPTIONS,
    EVENT_CODES,
    parse_event_code,
)


def test_known_code_is_unaffected_by_the_catalogue() -> None:
    """A mapped code keeps its action key, translated message and alarm flag."""
    parsed = parse_event_code("M_01_20")
    assert parsed is not None
    assert parsed["action"] == "door_opened"
    assert parsed["message"] == "Opening detected"  # translated, not the raw label
    assert parsed["category"] != "unknown"


def test_unmapped_code_shows_official_label_instead_of_raw_code() -> None:
    """The whole point: no more 'M_09_26' in the UI."""
    parsed = parse_event_code("M_09_26")
    assert parsed is not None
    assert parsed["message"] == "Smoke chamber dirty"
    assert parsed["device_type"] == "FireProtect Plus"


def test_unmapped_code_carries_no_alarm_semantics() -> None:
    """Display-only: the catalogue must never flip is_alarm or invent a category.

    ``is_alarm`` drives the space into TRIGGERED (see sqs_manager), so a label
    lookup must not be able to raise a false alarm.
    """
    parsed = parse_event_code("M_0C_32")  # "Gas leak detected"
    assert parsed is not None
    assert parsed["message"] == "Gas leak detected"
    assert parsed["is_alarm"] is False
    assert parsed["action"] == "unknown"
    assert parsed["category"] == "unknown"


def test_code_absent_everywhere_falls_back_to_the_raw_code() -> None:
    """Unknown to both tables: previous behaviour is preserved."""
    parsed = parse_event_code("M_ZZ_99")
    assert parsed is not None
    assert parsed["message"] == "M_ZZ_99"


def test_catalogue_does_not_shadow_mapped_codes() -> None:
    """The two tables are disjoint, so a mapped code can never lose its action key."""
    assert not (set(EVENT_CODES) & set(EVENT_CODE_DESCRIPTIONS))


def test_catalogue_labels_are_clean() -> None:
    """No leftover placeholders or scaffolding from Ajax's source file."""
    for code, label in EVENT_CODE_DESCRIPTIONS.items():
        assert "${" not in label, f"{code}: unresolved placeholder in {label!r}"
        assert "%1$s" not in label and "%3$s" not in label, f"{code}: printf placeholder in {label!r}"
        assert label == label.strip(), f"{code}: untrimmed {label!r}"
        assert "  " not in label, f"{code}: double space in {label!r}"
        assert label[0].isupper() or label[0].isdigit(), f"{code}: not capitalised: {label!r}"


def test_catalogue_has_no_french_rows() -> None:
    """Ajax's file mixes French into the English column; those rows are excluded."""
    french = re.compile(
        r"\b(le|la|les|des|est|dans|pour|avec|une|détecté|batterie|dispositif|perdue|connexion)\b",
        re.I,
    )
    offenders = [c for c, label in EVENT_CODE_DESCRIPTIONS.items() if len(french.findall(label)) >= 2]
    assert not offenders, f"French labels leaked into the catalogue: {offenders[:5]}"


def test_newly_added_device_types_resolve() -> None:
    """Device types Ajax added since the original table."""
    assert DEVICE_TYPES["40"] == "DoorBell"
    assert DEVICE_TYPES["7D"] == "SeismoProtect Fibra"
    assert DEVICE_TYPES["54"] == "SpeakerPhone Jeweller"
    # resolved through an event code, which is how the coordinator uses it
    parsed = parse_event_code("M_40_20")
    assert parsed is not None
    assert parsed["device_type"] == "DoorBell"
