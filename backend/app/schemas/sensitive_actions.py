from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from app.schemas.exports import QueryExportFilters

SensitiveActionPurpose = Literal[
    "export_all", "export_query", "personal_backup", "safety_backup", "legacy_import"
]


class SensitiveActionVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: SecretStr = Field(min_length=1, max_length=128)
    purpose: SensitiveActionPurpose
    preview_token: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{43}$")
    safety_backup_token: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{43}$")
    query_filters: QueryExportFilters | None = None

    @model_validator(mode="after")
    def validate_context(self):
        if (self.purpose == "legacy_import") != (self.preview_token is not None):
            raise ValueError("An import preview token is required only for legacy import")
        if (self.purpose == "safety_backup") != (self.safety_backup_token is not None):
            raise ValueError("A safety backup token is required only for its download")
        if (self.purpose == "export_query") != (self.query_filters is not None):
            raise ValueError("Query export must include the exact reviewed query")
        return self


class SensitiveActionGrantResponse(BaseModel):
    grant: str
    expires_at: datetime
    expires_in_seconds: int = 300
