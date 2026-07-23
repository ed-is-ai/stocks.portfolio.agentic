"""Integration settings routes — configure secrets without editing ``.env``.

Every mutation is guarded by ``require_local_or_token`` (loopback, or a
matching ``X-Auth-Token``, with same-origin CSRF checks) — see
``app.core.security``. Secret values are never echoed back in a response;
the one exception is a freshly rotated ``APP_AUTH_TOKEN``, which is
returned exactly once with ``Cache-Control: no-store``.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from app.api.dependencies import get_integration_config_service
from app.api.templating import templates
from app.core.security import require_local_or_token
from app.services.integration_config_service import (
    IntegrationConfigError,
    IntegrationConfigService,
)

router = APIRouter()

ConfigDep = Annotated[IntegrationConfigService, Depends(get_integration_config_service)]

_FORM_TO_ENV = {
    "fmp_api_key": "FMP_API_KEY",
    "alpha_vantage_api_key": "ALPHA_VANTAGE_API_KEY",
    "email_user": "EMAIL_USER",
    "email_password": "EMAIL_PASSWORD",
    "email_to": "EMAIL_TO",
    "email_host": "EMAIL_HOST",
    "email_port": "EMAIL_PORT",
}


def _settings_context(
    config: IntegrationConfigService, **updates: object
) -> dict[str, object]:
    context: dict[str, object] = {"fields": config.status()}
    context.update(updates)
    return context


@router.get("/partials/settings", response_class=HTMLResponse)
async def partial_settings(request: Request, config: ConfigDep) -> HTMLResponse:
    """Render the Settings/Integrations tab."""
    return templates.TemplateResponse(
        request, "_settings.html", context=_settings_context(config)
    )


@router.post(
    "/settings",
    response_class=HTMLResponse,
    dependencies=[Depends(require_local_or_token)],
)
async def save_settings(
    request: Request,
    config: ConfigDep,
    fmp_api_key: Annotated[str, Form()] = "",
    alpha_vantage_api_key: Annotated[str, Form()] = "",
    email_user: Annotated[str, Form()] = "",
    email_password: Annotated[str, Form()] = "",
    email_to: Annotated[str, Form()] = "",
    email_host: Annotated[str, Form()] = "",
    email_port: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Save any non-blank submitted fields; blank fields leave keys unchanged."""
    submitted = {
        "fmp_api_key": fmp_api_key,
        "alpha_vantage_api_key": alpha_vantage_api_key,
        "email_user": email_user,
        "email_password": email_password,
        "email_to": email_to,
        "email_host": email_host,
        "email_port": email_port,
    }
    values = {_FORM_TO_ENV[k]: v for k, v in submitted.items()}
    try:
        config.update(values)
        save_status, save_success = "Settings saved", True
    except IntegrationConfigError as exc:
        save_status, save_success = str(exc), False
    return templates.TemplateResponse(
        request,
        "_settings.html",
        context=_settings_context(
            config, save_status=save_status, save_success=save_success
        ),
    )


@router.post(
    "/settings/clear",
    response_class=HTMLResponse,
    dependencies=[Depends(require_local_or_token)],
)
async def clear_setting(
    request: Request,
    config: ConfigDep,
    key: Annotated[str, Form()],
) -> HTMLResponse:
    """Explicitly clear one previously configured key.

    A distinct, confirmed action from saving (the client prompts via
    ``hx-confirm`` before this request is ever sent) — a blank field on the
    save form is never enough to clear a value.
    """
    try:
        config.update({}, clear_keys=[key])
        save_status, save_success = f"{key} cleared", True
    except IntegrationConfigError as exc:
        save_status, save_success = str(exc), False
    return templates.TemplateResponse(
        request,
        "_settings.html",
        context=_settings_context(
            config, save_status=save_status, save_success=save_success
        ),
    )


@router.post(
    "/settings/token/rotate",
    response_class=HTMLResponse,
    dependencies=[Depends(require_local_or_token)],
)
async def rotate_token(request: Request, config: ConfigDep) -> HTMLResponse:
    """Generate and persist a new ``APP_AUTH_TOKEN``, revealed once."""
    token = config.rotate_token()
    response = templates.TemplateResponse(
        request,
        "_settings.html",
        context=_settings_context(config, revealed_token=token),
    )
    response.headers["Cache-Control"] = "no-store"
    return response
