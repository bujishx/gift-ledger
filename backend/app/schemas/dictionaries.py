from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class DictionaryCategory(StrEnum):
    RELATIONSHIP = "relationship"
    KINSHIP = "kinship"
    EVENT = "event"


class DictionaryCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(strict=True, min_length=1, max_length=50, description="自定义项名称")


class DictionaryRenameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(strict=True, min_length=1, max_length=50, description="新的自定义项名称")


class DictionaryItemResponse(BaseModel):
    id: int
    category: DictionaryCategory
    name: str
    is_system: bool
    is_active: bool
    editable: bool


class DictionaryListResponse(BaseModel):
    items: list[DictionaryItemResponse]
