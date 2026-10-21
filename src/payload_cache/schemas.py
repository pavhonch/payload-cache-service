from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

MAX_LIST_ITEMS = 1000
MAX_STRING_CHARACTERS = 10_000
MAX_REQUEST_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 4 * 1024 * 1024


def validate_unicode(value: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("String must contain valid Unicode characters") from exc
    return value


InputText = Annotated[
    str,
    Field(strict=True, max_length=MAX_STRING_CHARACTERS),
    AfterValidator(validate_unicode),
]
PayloadId = Annotated[
    str, Field(strict=True, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
]


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PayloadInput(Schema):
    list_1: list[InputText] = Field(max_length=MAX_LIST_ITEMS)
    list_2: list[InputText] = Field(max_length=MAX_LIST_ITEMS)

    @model_validator(mode="after")
    def validate_lengths(self) -> Self:
        if len(self.list_1) != len(self.list_2):
            raise ValueError("list_1 and list_2 must have the same length")
        return self


class PayloadConfirmation(Schema):
    id: PayloadId
    message: Literal["Payload is ready"] = "Payload is ready"


class PayloadOutput(Schema):
    output: Annotated[str, AfterValidator(validate_unicode)]


class HealthResponse(Schema):
    status: Literal["ok"] = "ok"
