from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field

from app.api.dependencies import SettingsDependency, database_is_ready

router = APIRouter(prefix="/health", tags=["健康检查"])


class HealthResponse(BaseModel):
    status: Literal["ok", "unavailable"] = Field(description="当前状态")
    service: str = Field(description="服务名称")
    version: str = Field(description="服务版本")


@router.get("/live", response_model=HealthResponse, summary="检查服务是否存活")
def live(settings: SettingsDependency) -> HealthResponse:
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        version=settings.app_version,
    )


@router.get(
    "/ready",
    response_model=HealthResponse,
    summary="检查服务和数据库是否就绪",
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": HealthResponse}},
)
def ready(
    response: Response,
    settings: SettingsDependency,
    database_ready: Annotated[bool, Depends(database_is_ready)],
) -> HealthResponse:
    if not database_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthResponse(
            status="unavailable",
            service=settings.app_name,
            version=settings.app_version,
        )

    return HealthResponse(
        status="ok",
        service=settings.app_name,
        version=settings.app_version,
    )
