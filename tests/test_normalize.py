from app.domain.normalize import (
    format_hours,
    normalize_title,
    parse_hours,
    parse_tag_list,
)


def test_normalize_title_strips_editions():
    assert normalize_title("The Witcher 3: Wild Hunt - Game of the Year Edition") == (
        "the witcher 3 wild hunt"
    )
    assert normalize_title("Dark Souls™ Remastered") == "dark souls"
    assert normalize_title("Hollow Knight") == "hollow knight"


def test_parse_tag_list_deduplicates():
    assert parse_tag_list("cozy, Story, cozy,  coop ") == ["cozy", "Story", "coop"]


def test_parse_hours():
    assert parse_hours("12,5") == 750
    assert parse_hours("3") == 180
    assert parse_hours("") == 0
    assert parse_hours("nope") == 0


def test_format_hours():
    assert format_hours(0) == "0 h"
    assert format_hours(90) == "1.5 h"
    assert format_hours(600) == "10 h"
