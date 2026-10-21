import pytest

from payload_cache.transformer import UppercaseTransformer


@pytest.mark.parametrize(
    ("source", "expected"),
    [("hello", "HELLO"), ("", ""), (" hi ", " HI "), ("Straße", "STRASSE"), ("a\x00b", "A\x00B")],
)
def test_uppercase_transformer(source: str, expected: str) -> None:
    assert UppercaseTransformer().transform(source) == expected
