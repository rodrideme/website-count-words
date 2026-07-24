from __future__ import annotations

import os

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from app import db
from app.models import User
from app.templates import templates

router = APIRouter()

oauth = OAuth()
oauth.register(
    name="google",
    client_id=os.environ["GOOGLE_CLIENT_ID"],
    client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


async def get_current_user(request: Request) -> User | None:
    user_id = request.session.get("user_id")
    if user_id is None:
        return None
    if not hasattr(request.state, "user_cache"):
        request.state.user_cache = await db.get_user(user_id)
    return request.state.user_cache


async def require_user(request: Request) -> User:
    """Use for HTML pages: bounces an anonymous browser to /login."""
    user = await get_current_user(request)
    if user is None:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


async def require_user_api(request: Request) -> User:
    """Use for JSON/SSE endpoints: a redirect isn't meaningful there."""
    user = await get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    """404, not 403 — a non-admin shouldn't be able to tell this route
    exists at all, not just that they're forbidden from it."""
    admin_emails = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}
    if user.email.lower() not in admin_emails:
        raise HTTPException(status_code=404)
    return user


@router.get("/login")
async def login_page(request: Request):
    error = request.query_params.get("error")
    return templates.TemplateResponse(request, "login.html", {"error": error})


@router.get("/auth/login")
async def auth_login(request: Request):
    redirect_uri = request.url_for("auth_callback")
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/auth/callback", name="auth_callback")
async def auth_callback(request: Request):
    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception:
        return RedirectResponse(url="/login?error=Google+sign-in+failed", status_code=302)

    userinfo = token.get("userinfo")
    if userinfo is None:
        return RedirectResponse(url="/login?error=Could+not+read+Google+profile", status_code=302)

    user = await db.get_or_create_user(
        google_sub=userinfo["sub"],
        email=userinfo.get("email", ""),
        name=userinfo.get("name") or userinfo.get("email", "User"),
        picture=userinfo.get("picture"),
    )
    request.session["user_id"] = user.id
    return RedirectResponse(url="/", status_code=302)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)
