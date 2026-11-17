# ruff: noqa: RUF002
"""个人备份恢复接口的请求与响应模型。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class PersonalRestoreCounts(BaseModel):
    """六项业务数据的数量。"""

    relationship_types: int
    kinship_types: int
    event_types: int
    contacts: int
    events: int
    gifts: int


class PersonalRestoreDictionaryConflict(BaseModel):
    """恢复预览中必须由用户确认改名的一项 S005 自定义字典冲突。"""

    category: Literal["relationship", "kinship", "event"]
    backup_id: int
    original_name: str
    suggested_name: str


class PersonalRestoreDictionaryRename(BaseModel):
    """恢复确认时提交的一项冲突改名。"""

    model_config = ConfigDict(extra="forbid")

    category: Literal["relationship", "kinship", "event"]
    backup_id: int = Field(gt=0)
    original_name: str = Field(min_length=1, max_length=50)
    new_name: str = Field(min_length=1, max_length=50)


class PersonalRestorePreviewResponse(BaseModel):
    """恢复预览：一次性确认 token、备份包信息、当前与替换后的六项数量及完整替换提示。"""

    preview_token: str
    expires_at: datetime
    format_version: int
    application_version: str
    created_at: datetime
    current: PersonalRestoreCounts
    incoming: PersonalRestoreCounts
    mode: str
    replace_notice: str
    dictionary_name_conflicts: list[PersonalRestoreDictionaryConflict]


class PersonalRestoreConfirmRequest(BaseModel):
    """确认恢复请求：当前密码及预览要求的全部冲突改名。"""

    model_config = ConfigDict(extra="forbid")

    password: SecretStr = Field(max_length=128)
    dictionary_renames: list[PersonalRestoreDictionaryRename] = Field(
        default_factory=list, max_length=100
    )


class PersonalRestoreConfirmResponse(BaseModel):
    """确认恢复成功：六项恢复数量、安全备份下载 token／过期时间与需重新登录标记。"""

    counts: PersonalRestoreCounts
    safety_backup_token: str
    safety_backup_expires_at: datetime
    reauth_required: bool


__all__ = [
    "PersonalRestoreConfirmRequest",
    "PersonalRestoreConfirmResponse",
    "PersonalRestoreCounts",
    "PersonalRestoreDictionaryConflict",
    "PersonalRestoreDictionaryRename",
    "PersonalRestorePreviewResponse",
]
