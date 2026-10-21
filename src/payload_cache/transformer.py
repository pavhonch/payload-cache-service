from typing import Protocol


class Transformer(Protocol):
    @property
    def version(self) -> str: ...

    def transform(self, value: str) -> str: ...


class UppercaseTransformer:
    @property
    def version(self) -> str:
        return "upper-v1"

    def transform(self, value: str) -> str:
        return value.upper()
