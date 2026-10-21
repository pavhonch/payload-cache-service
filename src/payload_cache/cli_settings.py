from typing import Self

from pydantic import Field, HttpUrl, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict


class CliSettings(BaseSettings):
    """Create and read cached payloads. Use -h for help and -H for the server URL."""

    model_config = SettingsConfigDict(
        cli_prog_name="cache-cli",
        cli_shortcuts={"host": "H", "repeat": "r", "input": "i", "json": "j", "output": "o"},
        cli_hide_none_type=True,
        cli_avoid_json=True,
        extra="forbid",
        case_sensitive=True,
    )

    host: HttpUrl = Field(default=HttpUrl("http://localhost:8000"), description="Server base URL")
    repeat: int = Field(default=1, gt=0, description="Number of POST/GET iterations")
    input_file: str | None = Field(
        default=None, alias="input", description="UTF-8 file, or - for stdin"
    )
    json_input: str | None = Field(default=None, alias="json", description="Inline JSON input")
    output_file: str = Field(default="-", alias="output", description="JSONL file, or - for stdout")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings,)

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: HttpUrl) -> HttpUrl:
        if any(
            part is not None
            for part in (value.username, value.password, value.query, value.fragment)
        ):
            raise ValueError("Host must not include credentials, a query, or a fragment")
        return value

    @field_validator("input_file", "output_file")
    @classmethod
    def validate_filename(cls, value: str | None) -> str | None:
        if value is not None and (not value or "\x00" in value):
            raise ValueError("File name must be nonempty and contain no NUL characters")
        return value

    @model_validator(mode="after")
    def validate_input_source(self) -> Self:
        if (self.input_file is None) == (self.json_input is None):
            raise ValueError("Provide exactly one of --input or --json")
        return self
