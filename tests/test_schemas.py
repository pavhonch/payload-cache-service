import pytest
from pydantic import ValidationError

from payload_cache.schemas import MAX_LIST_ITEMS, MAX_STRING_CHARACTERS, PayloadInput


def test_input_preserves_strings() -> None:
    values = ["", " hello ", "Привіт", "a\x00b", "a, b", "\U0001f680"]
    payload = PayloadInput(list_1=values, list_2=values)
    assert payload.list_1 == values
    assert payload.list_2 == values


def test_empty_lists_are_valid() -> None:
    assert PayloadInput(list_1=[], list_2=[]).list_1 == []


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"list_1": [], "list_2": ["a"]},
        {"list_1": "a", "list_2": ["b"]},
        {"list_1": [1], "list_2": ["b"]},
        {"list_1": [None], "list_2": ["b"]},
        {"list_1": ["a"], "list_2": ["b"], "extra": True},
        {"list_1": ["\ud800"], "list_2": ["b"]},
        {"list_1": ["a"] * (MAX_LIST_ITEMS + 1), "list_2": ["b"] * (MAX_LIST_ITEMS + 1)},
        {"list_1": ["a" * (MAX_STRING_CHARACTERS + 1)], "list_2": ["b"]},
    ],
)
def test_invalid_input_is_rejected(value: object) -> None:
    with pytest.raises(ValidationError):
        PayloadInput.model_validate(value)
