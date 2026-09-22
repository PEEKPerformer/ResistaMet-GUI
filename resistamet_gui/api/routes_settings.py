"""Users, profiles, settings resolution and instrument discovery.

The routes a client needs before it can start a run: who can run, what their
stored settings are, what a request would resolve to, and what is on the bus.

Resolution is exposed deliberately. A client should be able to ask "what would
this run actually use, and does it have problems?" without starting anything,
which is also how a UI shows validation before the Start button.
"""
import math
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, ValidationError

from .. import visa_backend
from ..schema.resolve import allowed_override_keys, resolve_run_settings
from ..schema.settings_common import (AuxSensorSettings, DisplaySettings, FileSettings,
                                       InstrumentSettings, OutputSettings, SafetySettings)
from ..schema.settings_modes import MODE_MODELS
from ..session.manager import MeasurementSession, SessionBusy
from .app import UI_ROLE, busy_as_conflict, get_session, require_token

router = APIRouter(tags=["settings"])

#: Keys that describe this machine rather than this profile.
MACHINE_LOCAL_KEYS = ('gpib_address', 'visa_library', 'gpib_interface')

#: Keys that decide whether the hazardous-voltage prompt is asked.
SAFETY_KEYS = tuple(SafetySettings.model_fields)

#: VISA backends a client may name. Anything else is a path that pyvisa hands
#: to ctypes, so it is only ever taken from this machine's stored settings.
NAMED_VISA_LIBRARIES = (visa_backend.AUTO, visa_backend.IVI, visa_backend.PY)

#: The models that describe each section of a stored profile.
SECTION_MODELS = {
    'measurement': (*MODE_MODELS.values(), InstrumentSettings, AuxSensorSettings,
                    SafetySettings),
    'display': (DisplaySettings,),
    'file': (FileSettings,),
    'output': (OutputSettings,),
}


class ResolveRequest(BaseModel):
    mode: str
    username: str = Field(min_length=1)
    overrides: Dict[str, Any] = Field(default_factory=dict)
    strict: bool = True


class ProfilePatch(BaseModel):
    measurement: Optional[Dict[str, Any]] = None
    display: Optional[Dict[str, Any]] = None
    file: Optional[Dict[str, Any]] = None
    output: Optional[Dict[str, Any]] = None

    def sections(self) -> Dict[str, Any]:
        return {name: value for name, value in self.model_dump().items() if value is not None}


class IdentifyRequest(BaseModel):
    address: str = Field(min_length=1)
    #: None = this machine's configured backend.
    visa_library: Optional[str] = None
    #: None = this machine's configured GPIB interface; '' = none.
    gpib_interface: Optional[str] = None


def _config(request: Request):
    return request.app.state.api.config


def _bus_overrides(request: Request, visa_library: Optional[str],
                   gpib_interface: Optional[str]):
    """The VISA backend and GPIB interface one bus request will use.

    None means this machine's stored setting. A request may try another
    backend before saving it, but only one of the named ones: a path would be
    loaded into this process. The stored value itself is always allowed, so a
    client can send back what the profile gave it.
    """
    config = _config(request)
    stored_library, stored_interface = config.get_visa_library(), config.get_gpib_interface()
    if visa_library is None:
        visa_library = stored_library
    elif visa_library not in NAMED_VISA_LIBRARIES and visa_library != stored_library:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="visa_library is '', '@ivi' or '@py' here; a library path is set "
                   "in this machine's settings")
    if gpib_interface is None:
        gpib_interface = stored_interface
    return visa_library, gpib_interface


@router.get("/users")
def list_users(request: Request, role: str = Depends(require_token)):
    return {"users": _config(request).config.get('users', []),
            "last_user": _config(request).config.get('last_user')}


class NewUser(BaseModel):
    username: str = Field(min_length=1, max_length=64)


@router.post("/users", status_code=status.HTTP_201_CREATED)
def add_user(body: NewUser, request: Request, role: str = Depends(require_token)):
    """Create a profile. Idempotent: an existing name is simply selected."""
    config = _config(request)
    username = body.username.strip()
    if not username:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                             detail="username is empty")
    config.add_user(username)
    config.set_last_user(username)
    return {"users": config.config.get('users', []), "last_user": username}


@router.get("/profiles/{username}")
def read_profile(username: str, request: Request, role: str = Depends(require_token)):
    return _config(request).get_user_settings(username)


def _section_issues(section: str, values: Dict[str, Any]) -> List[Dict[str, str]]:
    """What the schema models say about one section of a profile."""
    issues = []
    for model in SECTION_MODELS[section]:
        subset = {name: values[name] for name in model.model_fields if name in values}
        # "Not measured" is NaN in a stored profile and None to the model.
        if isinstance(subset.get('fpp_temperature_c'), float) and \
                math.isnan(subset['fpp_temperature_c']):
            subset['fpp_temperature_c'] = None
        try:
            model(**subset)
        except ValidationError as exc:
            for error in exc.errors():
                key = str(error['loc'][0]) if error['loc'] else model.__name__
                issue = {'section': section, 'key': key, 'message': error['msg']}
                if issue not in issues:
                    issues.append(issue)
    return issues


def _refuse_a_worse_profile(sections: Dict[str, Any], role: str):
    """The check a profile edit has to pass before it is stored.

    The touch-safety keys decide whether the hazardous-voltage prompt is ever
    asked, and only a person at the bench may answer that prompt (design
    decision D4). A role that may not answer it may not raise its threshold
    or silence it here either.

    An issue blocks the edit when it is on a key the edit changes, or when the
    profile did not have it before. One that was already there and is not
    being touched does not: a profile that drifted out of range long ago must
    still be editable, one key at a time.
    """
    def check(current: Dict[str, Any], merged: Dict[str, Any]) -> None:
        if role != UI_ROLE and any(
                current['measurement'].get(key) != merged['measurement'].get(key)
                for key in SAFETY_KEYS):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                 detail="the touch-safety settings can only be changed "
                                        "from the user interface")
        blocking = []
        library = merged['measurement'].get('visa_library', '')
        if library != current['measurement'].get('visa_library', '') \
                and library not in NAMED_VISA_LIBRARIES:
            # A path is loaded into this process the next time the bus opens.
            if role != UI_ROLE:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                     detail="a VISA library path can only be set from "
                                            "the user interface")
            if not os.path.isfile(str(library)):
                blocking.append({'section': 'measurement', 'key': 'visa_library',
                                 'message': f"no such file: {library}"})
        for section, sent in sections.items():
            before = current.get(section, {})
            changed = {key for key, value in sent.items()
                       if key not in before or before[key] != value}
            already = _section_issues(section, before)
            for issue in _section_issues(section, merged.get(section, {})):
                if issue['key'] in changed or issue not in already:
                    blocking.append(issue)
        if blocking:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                 detail={'message': 'the profile would not be valid',
                                         'issues': blocking})
    return check


@router.patch("/profiles/{username}")
def patch_profile(username: str, body: ProfilePatch, request: Request,
                   session: MeasurementSession = Depends(get_session),
                   role: str = Depends(require_token)):
    """Change the keys sent; every other key of the profile stays as stored."""
    sections = body.sections()
    if not sections:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                             detail="no sections to update")
    config = _config(request)
    if username not in config.get_users():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                             detail=f"no user named '{username}'")
    measurement = sections.get('measurement') or {}
    if any(key in measurement for key in MACHINE_LOCAL_KEYS) and session.state != 'idle':
        # Changing the address mid-run would describe a run that is not the
        # one on the bus.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                             detail="cannot change the instrument address during a run")
    return config.merge_user_settings(username, sections,
                                       check=_refuse_a_worse_profile(sections, role))


@router.get("/schema/settings")
def read_schema(role: str = Depends(require_token)):
    """What a client may send, per mode."""
    return {
        'modes': {mode: {
            'model': model.__name__,
            'fields': sorted(model.model_fields),
            'override_keys': sorted(allowed_override_keys(mode)),
        } for mode, model in MODE_MODELS.items()},
    }


@router.post("/settings/resolve")
def resolve(body: ResolveRequest, request: Request, role: str = Depends(require_token)):
    """Preview a run's settings without starting it."""
    if body.mode not in MODE_MODELS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                             detail=f"unknown mode '{body.mode}'")
    profile = _config(request).get_user_settings(body.username)
    resolved = resolve_run_settings(profile, body.mode, body.overrides, strict=body.strict)
    hazard = resolved.hazard
    return {
        'settings': resolved.settings,
        'derived': resolved.derived,
        'ok': resolved.ok,
        'issues': [{'key': i.key, 'message': i.message, 'severity': i.severity}
                    for i in resolved.issues],
        'hazard': None if hazard is None else {
            'hazardous': hazard.hazardous,
            'voltage_v': hazard.voltage_v,
            'threshold_v': hazard.threshold_v,
            'reason': hazard.reason,
        },
    }


@router.get("/instruments/resources")
def list_resources(request: Request, visa_library: Optional[str] = None,
                    gpib_interface: Optional[str] = None,
                    session: MeasurementSession = Depends(get_session),
                    role: str = Depends(require_token)):
    """What VISA can see. Refused during a run: enumerating touches the bus.

    Uses this machine's configured VISA backend unless ``visa_library`` is
    given, so a client can try a backend before saving it. The reply says
    which implementation actually answered. ``gpib_interface`` works the same
    way: the reply names the interface that is open, or None (none asked for,
    or a vendor library ignored it), and one that does not open is the 503.
    """
    if session.state != 'idle':
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                             detail=f"session is {session.state}")
    visa_library, gpib_interface = _bus_overrides(request, visa_library, gpib_interface)
    try:
        rm = visa_backend.resource_manager(visa_library, gpib_interface)
        resources = list(rm.list_resources())
    except visa_backend.GpibInterfaceError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                             detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                             detail=f"VISA unavailable: {exc}")
    return {"resources": resources,
            "backend": visa_backend.describe(rm, visa_library),
            "gpib_interface": visa_backend.held_gpib_interface(rm)}


@router.post("/instruments/identify")
def identify(body: IdentifyRequest, request: Request,
              session: MeasurementSession = Depends(get_session),
              role: str = Depends(require_token)):
    visa_library, gpib_interface = _bus_overrides(request, body.visa_library,
                                                  body.gpib_interface)
    try:
        return session.identify(body.address, visa_library, gpib_interface)
    except SessionBusy as exc:
        raise busy_as_conflict(exc)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                             detail=str(exc))
