"""Unit tests for find_course_relation_property — the function that locates
the Course relation's schema by type rather than by the configured
PROP_COURSE name, because (per its own docstring in sync.py) Notion's
data-source schema shape has shifted more than once. This is a pure
function over a `properties` dict, so it's tested directly without any
fake client.
"""

import pytest

import sync


def test_finds_relation_property_matching_configured_name():
    """Given a property named "Course" of type relation, it is returned directly."""
    props = {
        "Course": {"type": "relation", "relation": {"database_id": "courses-db"}},
        "Task name": {"type": "title"},
    }
    result = sync.find_course_relation_property(props)
    assert result["relation"]["database_id"] == "courses-db"


def test_falls_back_to_sole_relation_property_when_name_differs():
    """Given no property literally named "Course" but exactly one relation-typed property,
    that property is used instead (schema-shape-change tolerance)."""
    props = {
        "Linked Course": {"type": "relation", "relation": {"database_id": "courses-db"}},
        "Task name": {"type": "title"},
    }
    result = sync.find_course_relation_property(props)
    assert result["relation"]["database_id"] == "courses-db"


def test_raises_when_no_relation_property_exists():
    """Given a schema with zero relation-typed properties, a RuntimeError is raised naming what was found."""
    props = {"Task name": {"type": "title"}, "Status": {"type": "status"}}
    with pytest.raises(RuntimeError):
        sync.find_course_relation_property(props)


def test_raises_when_multiple_relation_properties_are_ambiguous():
    """Given two relation-typed properties and neither named "Course", the lookup can't disambiguate and raises."""
    props = {
        "Course": {"type": "select"},  # named right, but wrong type -- doesn't count
        "Related Tasks": {"type": "relation", "relation": {"database_id": "a"}},
        "Related Notes": {"type": "relation", "relation": {"database_id": "b"}},
    }
    with pytest.raises(RuntimeError):
        sync.find_course_relation_property(props)
