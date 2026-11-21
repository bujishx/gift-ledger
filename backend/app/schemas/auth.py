from pydantic import BaseModel, ConfigDict, Field


class CredentialsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(strict=True, min_length=1, max_length=30, description="登录名")
    password: str = Field(strict=True, min_length=1, max_length=128, description="密码")


class RegistrationRequest(CredentialsRequest):
    invite_code: str | None = Field(default=None, strict=True, max_length=128)


class GiftChangeVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(strict=True, min_length=1, max_length=128, description="当前用户密码")


class AccountResponse(BaseModel):
    id: int
    username: str
    is_admin: bool
    profile_completed: bool
    display_name: str | None


class MessageResponse(BaseModel):
    message: str
