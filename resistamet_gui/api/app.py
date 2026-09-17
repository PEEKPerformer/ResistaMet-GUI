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
from typing import Callable, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..session.manager import MeasurementSession, SessionBusy

logger = logging.getLogger(__name__)

UI_ROLE = 'ui'

_bearer = HTTPBearer(auto_error=True)


class ApiState:
    """What the routes share: the session, the token, its role, the profiles.

    ``profile_provider`` maps a username to the stored settings a run starts
    from. It is injected rather than reached for, so tests do not need a
    config file and the sidecar decides which config it reads.
    """

    def __init__(self, session: MeasurementSession, token: str, role: str = UI_ROLE,
                 profile_provider: Optional[Callable[[str], dict]] = None):
        self.session = session
        self.token = token
        self.role = role
        self.profile_provider = profile_provider or _config_profiles


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


def _config_profiles(username: str) -> dict:
    """Default provider: the profile ConfigManager has on disk."""
    from ..config import ConfigManager

    return ConfigManager().get_user_settings(username)


def create_app(session: MeasurementSession, token: Optional[str] = None,
                role: str = UI_ROLE,
                profile_provider: Optional[Callable[[str], dict]] = None) -> FastAPI:
    """Build the app around an existing session."""
    from .routes_session import router as session_router

    app = FastAPI(title="ResistaMet", version="2.0-dev")
    app.state.api = ApiState(session, token or secrets.token_urlsafe(32), role,
                              profile_provider)
    app.include_router(session_router)

    @app.get("/health")
    def health():
        """Unauthenticated: liveness only, nothing about the instrument."""
        return {"status": "ok"}

    return app
