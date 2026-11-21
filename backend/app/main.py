import os
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.dependencies import require_local_entry
from app.api.errors import register_error_handlers
from app.api.routes import site_about
from app.api.routes.admin_backups import router as admin_backups_router
from app.api.routes.auth import router as auth_router
from app.api.routes.contacts import router as contacts_router
from app.api.routes.dictionaries import router as dictionaries_router
from app.api.routes.exports import router as exports_router
from app.api.routes.health import router as health_router
from app.api.routes.home import router as home_router
from app.api.routes.imports import router as imports_router
from app.api.routes.ledger import router as ledger_router
from app.api.routes.ledger_queries import router as ledger_queries_router
from app.api.routes.maintenance_accounts import router as maintenance_accounts_router
from app.api.routes.maintenance_enrollment import router as maintenance_enrollment_router
from app.api.routes.maintenance_login import router as maintenance_login_router
from app.api.routes.maintenance_registration import router as maintenance_registration_router
from app.api.routes.maintenance_restore import router as maintenance_restore_router
from app.api.routes.personal_restore import router as personal_restore_router
from app.api.routes.profile import router as profile_router
from app.api.routes.site_maintenance import router as site_maintenance_router
from app.api.routes.statistics import router as statistics_router
from app.core.config import Settings, get_settings
from app.core.logging import RequestLoggingMiddleware, configure_logging
from app.services.gift_change_auth import GiftChangeReauthStore
from app.services.registration_limits import FailureLimiter
from app.services.sensitive_actions import SensitiveActionStore


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or get_settings()
    configure_logging(app_settings.log_level)

    application = FastAPI(
        title=app_settings.app_name,
        version=app_settings.app_version,
        description="往来有礼人情往来管理系统后端接口。",
    )
    application.state.settings = app_settings
    application.state.registration_limits = FailureLimiter()
    application.state.gift_change_reauth_store = GiftChangeReauthStore()
    application.state.sensitive_action_store = SensitiveActionStore()
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(app_settings.trusted_origin_list),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-CSRF-Token", "X-Request-ID", "X-Operation-Grant"],
        expose_headers=["Content-Disposition", "X-Request-ID"],
    )
    application.add_middleware(RequestLoggingMiddleware)
    register_error_handlers(application)
    private = APIRouter(dependencies=[Depends(require_local_entry)])
    maintenance_dir = os.environ.get("GIFTLEDGER_MAINTENANCE_DIR")
    if maintenance_dir and Path(maintenance_dir).is_absolute():
        application.state.maintenance_root = Path(maintenance_dir)
        private.include_router(site_maintenance_router)
    owner_dir = os.environ.get("GIFTLEDGER_OWNER_STATE_DIR")
    if owner_dir is not None:
        if not Path(owner_dir).is_absolute() or not hasattr(application.state, "maintenance_root"):
            raise ValueError(
                "Owner sessions require absolute site maintenance and state directories"
            )
        application.state.maintenance_owner_dir = Path(owner_dir)
        private.include_router(site_about.owner)
        private.include_router(maintenance_login_router)
        private.include_router(maintenance_accounts_router)
        private.include_router(maintenance_registration_router)
        private.include_router(maintenance_enrollment_router)
        private.include_router(maintenance_restore_router)
    private.include_router(personal_restore_router, prefix="/api/v1")
    private.include_router(admin_backups_router, prefix="/api/v1")
    application.include_router(private)
    application.include_router(site_about.business, prefix="/api/v1")
    application.include_router(health_router, prefix="/api/v1")
    application.include_router(home_router, prefix="/api/v1")
    application.include_router(auth_router, prefix="/api/v1")
    application.include_router(profile_router, prefix="/api/v1")
    application.include_router(dictionaries_router, prefix="/api/v1")
    application.include_router(exports_router, prefix="/api/v1")
    application.include_router(contacts_router, prefix="/api/v1")
    application.include_router(imports_router, prefix="/api/v1")
    application.include_router(ledger_router, prefix="/api/v1")
    application.include_router(ledger_queries_router, prefix="/api/v1")
    application.include_router(statistics_router, prefix="/api/v1")
    return application


app = create_app()
