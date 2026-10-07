"""The localhost API the Tauri shell and the MCP layer will both talk to.

A thin skin over :class:`~resistamet_gui.session.manager.MeasurementSession`:
every handler is a one-liner that validates, calls a session method, and turns
``SessionBusy`` into 409. Nothing about how a measurement works lives here.

Security posture, deliberately small: bind 127.0.0.1 and require a bearer
token minted per launch and handed to the child process on stdout. That keeps
any other local process from driving the instrument, which is the threat that
matters for a lab PC. It is not an authentication system and does not pretend
to be one.

Each token carries a role. The token the parent is handed is the ``ui``
role, which is the only role allowed to answer a prompt marked
``requires_human`` — claiming leads were rewired is not something software can
truthfully do (design doc, decision D4). A second token, the ``agent`` role,
exists only while this machine allows AI agents to connect (``agent_access``,
``docs/design/mcp_layer.md`` M2). Every role check asks for ``ui``, so a
token of any other role is refused wherever a person is required.
"""
import logging
import secrets
import threading
from typing import Callable, Dict, Iterable, Optional

import json
import math

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..config import ConfigSaveError
from ..schema.agent_limits import AgentLimitCheck, check_agent_limits
from ..session.manager import MeasurementSession, SessionBusy
from .agent_access import AgentAccess

logger = logging.getLogger(__name__)

UI_ROLE = 'ui'
#: The role of the token an MCP server presents on an agent's behalf.
AGENT_ROLE = 'agent'

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
    """What the routes share: the session, the tokens and their roles, the profiles.

    ``profile_provider`` maps a username to the stored settings a run starts
    from. It is injected rather than reached for, so tests do not need a
    config file and the sidecar decides which config it reads.

    ``agent_access`` adds and withdraws the ``agent`` token; it starts off.
    ``connection_file`` is where it writes that token for an MCP server.
    """

    def __init__(self, session: MeasurementSession, token: str, role: str = UI_ROLE,
                 profile_provider: Optional[Callable[[str], dict]] = None,
                 config=None, hub=None, connection_file: Optional[str] = None):
        self.session = session
        self.hub = hub
        # Replaced, never changed in place: a request thread checking a token
        # iterates whichever map it read, while another thread adds or
        # removes one.
        self._tokens: Dict[str, str] = {token: role}
        self._tokens_lock = threading.Lock()
        self._config = config
        self._profile_provider = profile_provider
        self.agent_access = AgentAccess(
            grant=lambda agent_token: self.add_token(agent_token, AGENT_ROLE),
            revoke=self.remove_token, path=connection_file)

    def add_token(self, token: str, role: str) -> None:
        with self._tokens_lock:
            self._tokens = {**self._tokens, token: role}

    def remove_token(self, token: str) -> None:
        """Forget a token. A request presenting it from now on is a 401."""
        with self._tokens_lock:
            self._tokens = {known: role for known, role in self._tokens.items()
                            if known != token}

    def role_for(self, presented: str) -> Optional[str]:
        """The role of the token presented, or None when it is not one of ours.

        Compared with every known token, in constant time each, and without
        stopping at a match: how long the check takes says as little as
        possible about which token, if any, was nearly right.
        """
        presented_bytes = presented.encode()
        matched = None
        for token, role in self._tokens.items():
            if secrets.compare_digest(presented_bytes, token.encode()):
                matched = role
        return matched

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
    def stored_config(self):
        """The ConfigManager, unless the app was given profiles and no config.

        A caller that injects ``profile_provider`` has said where settings
        come from. A route that only needs to know where an operator's files
        are must not open the ambient ``./config.json`` behind its back.
        """
        if self._config is None and self._profile_provider is not None:
            return None
        return self.config

    @property
    def profile_provider(self):
        return self._profile_provider or self.config.get_user_settings


def require_token(request: Request,
                   credentials: HTTPAuthorizationCredentials = Depends(_bearer)) -> str:
    """Check the bearer token; return the role it carries."""
    state: ApiState = request.app.state.api
    role = state.role_for(credentials.credentials)
    if role is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                             detail="invalid token")
    return role


def get_session(request: Request) -> MeasurementSession:
    return request.app.state.api.session


def agent_limit_verdict(session: MeasurementSession, profile: dict, mode: str,
                         settings: dict) -> AgentLimitCheck:
    """Whether a run with these resolved settings is within an agent's reach.

    The profile's ``agent_limits``, and the connected model's limits when the
    backend knows which model is at the run's address: the last ``identify``
    or run told it (``session.status()['instrument']``). When it does not
    know -- nothing has spoken to that address yet -- only the profile is
    checked, and the instrument enforces its own limits when the run
    configures it. Shared by start and resolve, so a dry run gives the
    verdict the start would.
    """
    instrument = session.status()['instrument']
    address = settings.get('measurement', {}).get('gpib_address')
    if instrument is not None and instrument.get('address') != address:
        instrument = None
    return check_agent_limits(settings, mode, profile.get('agent_limits'), instrument)


def busy_as_conflict(exc: SessionBusy) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


def _default_config():
    """The config file the sidecar was pointed at."""
    from ..config import ConfigManager

    # persist_on_open=False: this is built the first time a route wants it,
    # which may be a GET, and a read must not rewrite the file.
    return ConfigManager(raise_on_save_error=True, persist_on_open=False)


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
                allowed_origins: Optional[Iterable[str]] = None,
                connection_file: Optional[str] = None) -> FastAPI:
    """Build the app around an existing session.

    Agent access starts off; the caller turns it on through
    ``app.state.api.agent_access`` (the sidecar does, when this machine allows
    agents or ``--allow-agents`` was given).
    """
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
                              profile_provider, config, hub or EventHub(), connection_file)
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
