"""The localhost API the Tauri shell and the MCP layer will both talk to.

A thin skin over :class:`~resistamet_gui.session.manager.MeasurementSession`:
every handler is a one-liner that validates, calls a session method, and turns
``SessionBusy`` into 409. Nothing about how a measurement works lives here.

Security posture, deliberately small: bind 127.0.0.1 and require a bearer
token minted per launch and handed to the child process on stdout. That keeps
any other local process from driving the instrument, which is the threat that
matters for a lab PC. It is not an authentication system and does not pretend
to be one.

The token carries a role. In step 1 there is exactly one token and it is the
``ui`` role, which is the only role allowed to answer a prompt marked
``requires_human`` — claiming leads were rewired is not something software can
truthfully do (design doc, decision D4).
"""
import logging
import secrets
from typing import Callable, Iterable, Optional

import json
import math

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..config import ConfigSaveError
from ..session.manager import MeasurementSession, SessionBusy

logger = logging.getLogger(__name__)

UI_ROLE = 'ui'

_bearer = HTTPBearer(auto_error=True)


def _json_safe(value):
    """Replace non-finite floats with null, recursively.

    Settings and results legitimately carry NaN — an unmeasured temperature, a
    sigma that could not be computed — and JSON has no NaN. The event contract
    already serializes them as null; the HTTP routes match, so a client sees
    one representation of "no value" everywhere.
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


class NullNanJSONResponse(JSONResponse):
    """JSONResponse that renders non-finite floats as null."""

    def render(self, content) -> bytes:
        return json.dumps(_json_safe(content), allow_nan=False,
                           separators=(",", ":")).encode("utf-8")


class ApiState:
    """What the routes share: the session, the token, its role, the profiles.

    ``profile_provider`` maps a username to the stored settings a run starts
    from. It is injected rather than reached for, so tests do not need a
    config file and the sidecar decides which config it reads.
    """

    def __init__(self, session: MeasurementSession, token: str, role: str = UI_ROLE,
                 profile_provider: Optional[Callable[[str], dict]] = None,
                 config=None, hub=None):
        self.session = session
        self.hub = hub
        self.token = token
        self.role = role
        self._config = config
        self._profile_provider = profile_provider

    @property
    def config(self):
        """The ConfigManager, built on first use.

        Lazily, because constructing one opens (and may migrate) a config file:
        a caller that supplies its own profiles — a test, or a sidecar told to
        read a specific file — must not touch the ambient config.json just by
        creating the app.
        """
        if self._config is None:
            self._config = _default_config()
        return self._config

    @property
    def profile_provider(self):
        return self._profile_provider or self.config.get_user_settings


def require_token(request: Request,
                   credentials: HTTPAuthorizationCredentials = Depends(_bearer)) -> str:
    """Check the bearer token; return the role it carries."""
    state: ApiState = request.app.state.api
    if not secrets.compare_digest(credentials.credentials, state.token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                             detail="invalid token")
    return state.role


def get_session(request: Request) -> MeasurementSession:
    return request.app.state.api.session


def busy_as_conflict(exc: SessionBusy) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


def _default_config():
    """The config file the sidecar was pointed at."""
    from ..config import ConfigManager

    return ConfigManager(raise_on_save_error=True)


#: Origins the desktop shell and the UI dev server load the page from.
DEFAULT_ALLOWED_ORIGINS = (
    "tauri://localhost",
    "http://tauri.localhost",
    "https://tauri.localhost",
    "http://localhost:1420",
    "http://127.0.0.1:1420",
)


def create_app(session: MeasurementSession, token: Optional[str] = None,
                role: str = UI_ROLE,
                profile_provider: Optional[Callable[[str], dict]] = None,
                config=None, hub=None,
                allowed_origins: Optional[Iterable[str]] = None) -> FastAPI:
    """Build the app around an existing session."""
    from .event_hub import EventHub
    from .events_ws import router as events_router
    from .routes_maps import router as maps_router
    from .routes_results import router as results_router
    from .routes_session import router as session_router
    from .routes_settings import router as settings_router
    from .routes_spots import router as spots_router

    app = FastAPI(title="ResistaMet", version="2.0-dev",
                   default_response_class=NullNanJSONResponse)
    # The webview is a different origin from the backend — tauri://localhost in
    # the packaged app, the Vite dev server during UI work — so the browser
    # needs to be told the cross-origin call is expected. The list is closed:
    # a page from anywhere else still cannot reach the instrument.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(allowed_origins or DEFAULT_ALLOWED_ORIGINS),
        allow_methods=["*"],
        allow_headers=["Authorization", "Content-Type"],
    )
    app.state.api = ApiState(session, token or secrets.token_urlsafe(32), role,
                              profile_provider, config, hub or EventHub())
    app.include_router(session_router)
    app.include_router(settings_router)
    app.include_router(results_router)
    app.include_router(maps_router)
    app.include_router(spots_router)
    app.include_router(events_router)

    @app.exception_handler(RequestValidationError)
    async def _unprocessable(request: Request, exc: RequestValidationError):
        # FastAPI's own 422 body, rendered the way every other reply here is.
        # The errors echo the offending input, and an input of Infinity or NaN
        # -- refused for being exactly that -- cannot be written as JSON: the
        # stock handler raised while reporting it and the client got a 500.
        return NullNanJSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                   content={"detail": jsonable_encoder(exc.errors())})

    @app.exception_handler(ConfigSaveError)
    async def _settings_not_saved(request: Request, exc: ConfigSaveError):
        # The change is in memory and not on disk. Say so, rather than let a
        # client believe a profile edit will still be there tomorrow.
        return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            content={"detail": f"settings were not saved: {exc}"})

    @app.on_event("startup")
    async def _bind_hub():  # noqa: D401 - FastAPI hook
        # The hub needs the serving loop to hand events over from the run
        # thread; it only exists once the app is running.
        import asyncio

        app.state.api.hub.bind(asyncio.get_running_loop())

    @app.get("/health")
    def health():
        """Unauthenticated: liveness only, nothing about the instrument."""
        return {"status": "ok"}

    return app
