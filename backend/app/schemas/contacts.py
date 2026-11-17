from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ContactCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(strict=True, min_length=1, max_length=50, description="联系人姓名")
    relationship_type_id: int = Field(strict=True, gt=0, description="关系类型编号")
    location: str | None = Field(default=None, strict=True, max_length=50, description="常住地")
    phone: str | None = Field(default=None, strict=True, max_length=20, description="电话")
    remark: str | None = Field(default=None, strict=True, description="备注")


class ContactUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, strict=True, max_length=50)
    relationship_type_id: int | None = Field(default=None, strict=True, gt=0)
    location: str | None = Field(default=None, strict=True, max_length=50)
    phone: str | None = Field(default=None, strict=True, max_length=20)
    remark: str | None = Field(default=None, strict=True)

    @model_validator(mode="after")
    def require_at_least_one_field(self) -> "ContactUpdateRequest":
        if not self.model_fields_set:
            raise ValueError("至少需要提交一个要修改的字段")
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("联系人姓名不能设为空")
        if "relationship_type_id" in self.model_fields_set and self.relationship_type_id is None:
            raise ValueError("关系类型不能设为空")
        return self


class ContactResponse(BaseModel):
    id: int
    name: str
    relationship_type_id: int
    relationship_type_name: str
    location: str | None
    phone: str | None
    remark: str | None
    is_active: bool
    is_self: bool
    has_references: bool = False
    created_at: datetime | None = None


class ContactListResponse(BaseModel):
    items: list[ContactResponse]
    total: int
    limit: int
    offset: int


class ContactNameMatchesResponse(BaseModel):
    items: list[ContactResponse]
