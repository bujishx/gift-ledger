from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProfileCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(strict=True, min_length=1, max_length=50, description="本人真实姓名")
    location: str | None = Field(
        default=None,
        strict=True,
        max_length=50,
        description="常住地",
    )


class ProfileUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(
        default=None,
        strict=True,
        max_length=50,
        description="本人真实姓名",
    )
    location: str | None = Field(
        default=None,
        strict=True,
        max_length=50,
        description="常住地；传 null 或空白可清除",
    )

    @model_validator(mode="after")
    def require_at_least_one_field(self) -> "ProfileUpdateRequest":
        if not self.model_fields_set:
            raise ValueError("至少需要提交一个要修改的字段")
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("姓名不能设为空")
        return self


class ProfileResponse(BaseModel):
    id: int
    name: str
    location: str | None
