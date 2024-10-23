__all__ = ["router", "oauth"]

import traceback
from typing import Literal

from authlib.integrations.base_client import OAuthError
from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter
from starlette.datastructures import URL
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from src.config import settings
from src.exceptions import InvalidReturnToURL
from src.logging_ import logger
from src.modules.providers.innopolis.schemas import UserInfoFromSSO
from src.modules.users.repository import user_repository

router = APIRouter(prefix="/innopolis")

if settings.innopolis_sso:
    oauth = OAuth()

    innopolis_sso = oauth.register(
        "innopolis",
        client_id=settings.innopolis_sso.client_id,
        client_secret=settings.innopolis_sso.client_secret.get_secret_value(),
        # OAuth client will fetch configuration on first request
        server_metadata_url="https://sso.university.innopolis.ru/adfs/.well-known/openid-configuration",
        client_kwargs={"scope": "openid", "resource": settings.innopolis_sso.resource_id},
    )

    # Add type hinting
    oauth.innopolis: oauth.oauth2_client_cls  # noqa

    @router.get("/login")
    async def innopolis_login_or_register(redirect_uri: str, request: Request, prompt: Literal["none"] | None = None):
        ensure_allowed_redirect_uri(redirect_uri)
        request.session.clear()  # Clear session cookie as it is used only during auth
        request.session["redirect_uri"] = redirect_uri
        if prompt is not None:
            request.session["prompt"] = prompt
            return await oauth.innopolis.authorize_redirect(request, settings.innopolis_sso.redirect_uri, prompt=prompt)
        else:
            return await oauth.innopolis.authorize_redirect(request, settings.innopolis_sso.redirect_uri)

    @router.get("/callback")
    async def innopolis_callback(request: Request):
        # Check if there is any error from SSO
        error = request.query_params.get("error")
        if error:
            description = request.query_params.get("error_description")
            prompt = request.session.get("prompt")
            if prompt == "none":
                redirect_uri = request.session.pop("redirect_uri")
                ensure_allowed_redirect_uri(redirect_uri)
                request.session.clear()
                return RedirectResponse(redirect_uri, status_code=302)
            return JSONResponse(status_code=403, content={"error": error, "description": description})
        try:
            token = await oauth.innopolis.authorize_access_token(request)
        except OAuthError:
            logger.warning("OAuth error")
            traceback.print_exc()
            # Session is different on 'login' and 'callback'
            return await recover_mismatching_state(request)

        user_info_dict: dict = token["userinfo"]
        logger.debug(f"User info from SSO: {user_info_dict}")
        logger.debug(f"Token from SSO: {token}")
        user_info = UserInfoFromSSO.from_token_and_userinfo(token, user_info_dict)
        user = await user_repository.register_or_update_via_innopolis_sso(user_info)
        redirect_uri = request.session.pop("redirect_uri")
        ensure_allowed_redirect_uri(redirect_uri)
        request.session.clear()  # Clear session cookie as it is used only during auth
        request.session["uid"] = str(user.id)
        return RedirectResponse(redirect_uri, status_code=302)

    async def recover_mismatching_state(request: Request):
        redirect_uri = request.session.get("redirect_uri")
        user_id = request.session.get("uid")

        if user_id is not None:
            # The user has already authenticated in another tab,
            if redirect_uri:
                # And we know where to return a user. Let's get them where they want to be.
                ensure_allowed_redirect_uri(redirect_uri)
                return RedirectResponse(redirect_uri, status_code=302)
            else:
                # But we don't know where to return a user. Let's return the user to the main page.
                return RedirectResponse(settings.web_url, status_code=302)
        else:
            # User is not authenticated,
            if redirect_uri:
                # And we know where to return a user after authentication. Let's ask them to authenticate again.
                ensure_allowed_redirect_uri(redirect_uri)
                url = request.url_for("innopolis_login_or_register").include_query_params(redirect_uri=redirect_uri)
                return RedirectResponse(url, status_code=302)
            else:
                # We don't know anything, so let's return the user to the main page.
                return RedirectResponse(settings.web_url, status_code=302)

    def ensure_allowed_redirect_uri(return_to: str):
        try:
            url = URL(return_to)
            if url.hostname is None:
                return  # Ok. Allow returning to the current domain
            if url.hostname in settings.auth.allowed_domains:
                return  # Ok. Hostname is allowed (does not check port)
        except (AssertionError, ValueError):
            pass  # Bad. URL is malformed
        raise InvalidReturnToURL()
