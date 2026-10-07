"""The tools an agent sees, one per backend route (``docs/design/mcp_layer.md`` §6).

Each tool is a thin call: arguments become a request, the reply becomes a
compact JSON result. The rules live in the backend, which applies them to
the ``agent`` token whatever this server does; a refusal comes back as a
tool error that carries the backend's ``detail`` unchanged, because that
detail is what tells the agent which setting to change (an agent-limit
violation names the key, the value and the limit).

The descriptions are part of the contract. They say which unit a value is
in, that the agent limits exist and only a person can change them, that
``check_settings`` comes before ``start_run``, and that a prompt marked
``requires_human`` is for the person at the window. An agent learns the
rules from the tools, not from trying.

What is deliberately missing: raw SCPI, answering a prompt (every prompt
today needs a person), shutting the backend down (it belongs to the window),
and profile edits (the API allows them; a tool may follow).
"""
from __future__ import annotations

import json
from typing import Annotated, Any, Dict, Optional
from urllib.parse import quote

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

from .client import Backend, BackendError, BackendUnavailable

#: Said wherever a prompt needs a person, so the agent can pass it on.
PERSON_MUST_ANSWER = ("A person must answer this at the ResistaMet window; an agent "
                      "cannot. Tell the user what it asks, then wait_for the run.")

MODES = "resistance, source_v, source_i, four_point, sweep, vdp"

#: Read-only, and safe to repeat.
READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True,
                       open_world_hint=False)

User = Annotated[str, Field(description="Operator name, as list_users gives it. Their "
                                        "profile supplies every setting not overridden.")]
Mode = Annotated[str, Field(description=f"Measurement mode: one of {MODES}.")]
Overrides = Annotated[Optional[Dict[str, Any]], Field(
    description="Measurement keys to change for this run only, e.g. "
                "{\"res_test_current\": 0.001}. SI units, as the key names say: "
                "volts, amperes, seconds, hertz, hours (…_hours), cm/um/mm where named. "
                "Numbers must be JSON numbers, booleans JSON booleans. describe_mode "
                "lists the keys a mode accepts.")]


def result(value: Dict[str, Any]) -> CallToolResult:
    """A tool result: compact JSON text for the model, the same as structured data."""
    text = json.dumps(value, ensure_ascii=False, separators=(',', ':'), default=str)
    return CallToolResult(content=[TextContent(type='text', text=text)],
                          structured_content=value)


def explain(error: BackendError) -> str:
    """A refusal in words an agent can act on, with the backend's detail intact."""
    detail = error.detail if isinstance(error.detail, str) else json.dumps(
        error.detail, ensure_ascii=False, separators=(',', ':'))
    if error.status == 403:
        return ("Only a person at the ResistaMet window may do this (HTTP 403). "
                f"Ask the user to do it there. The backend said: {detail}")
    if error.status == 409:
        return (f"ResistaMet is busy (HTTP 409): {detail}. get_status shows what it is "
                "doing; stop_run ends a run.")
    if error.status == 422:
        message = f"ResistaMet refused the request (HTTP 422): {detail}"
        if isinstance(error.detail, dict) and error.detail.get('violations'):
            message += (" Agent limits are set per profile and only a person can change "
                        "them, at the ResistaMet window; change the run to fit, or ask.")
        return message
    return f"ResistaMet answered HTTP {error.status}: {detail}"


async def ask(backend: Backend, method: str, path: str, **kwargs) -> Any:
    """One backend request, with any failure turned into a tool error."""
    try:
        return await backend.call(method, path, **kwargs)
    except BackendUnavailable as exc:
        raise ToolError(str(exc)) from None
    except BackendError as exc:
        raise ToolError(explain(exc)) from None


def status_view(status: Dict[str, Any]) -> Dict[str, Any]:
    """``GET /session`` with a prompt that needs a person said to need one."""
    view = dict(status)
    prompt = view.get('pending_prompt')
    if isinstance(prompt, dict) and prompt.get('requires_human'):
        view['pending_prompt'] = {**prompt, 'who_answers': PERSON_MUST_ANSWER}
    return view


async def override_keys(backend: Backend, mode: str) -> Dict[str, Any]:
    """The schema route's entry for one mode, or a tool error naming the modes."""
    modes = (await ask(backend, 'GET', '/schema/settings')).get('modes', {})
    if mode not in modes:
        raise ToolError(f"unknown mode '{mode}'; the modes are: {', '.join(sorted(modes))}")
    return modes[mode]


def register(server: MCPServer, backend: Backend) -> None:
    """Add every tool to ``server``."""
    _register_reads(server, backend)


def _register_reads(server: MCPServer, backend: Backend) -> None:

    async def get_status() -> CallToolResult:
        health = await ask(backend, 'GET', '/health')
        status = status_view(await ask(backend, 'GET', '/session'))
        return result({'backend': health.get('status'), **status})

    server.add_tool(get_status, annotations=READ, title="Status", description=(
        "What ResistaMet is doing now: state (idle, identifying, running, paused, "
        "awaiting_prompt, stopping), the current or last run (run_id, mode, started_by, "
        "data file path), the instrument last connected (model and its max_source_v in V, "
        "max_source_i in A, max_power_w in W), and any pending prompt. A prompt with "
        "requires_human true can only be answered by a person at the ResistaMet window."))

    async def list_instruments() -> CallToolResult:
        return result(await ask(backend, 'GET', '/instruments/resources'))

    server.add_tool(list_instruments, annotations=READ, title="List instruments",
                    description=(
        "VISA resources this machine can see, and which VISA implementation answered. "
        "Only while idle: a scan puts traffic on the bus. The address a run uses is the "
        "profile's gpib_address, set by a person."))

    async def identify_instrument(
            address: Annotated[str, Field(description="VISA address, e.g. GPIB0::24::INSTR")],
    ) -> CallToolResult:
        return result(await ask(backend, 'POST', '/instruments/identify',
                                json_body={'address': address}))

    server.add_tool(identify_instrument, annotations=READ, title="Identify instrument",
                    description=(
        "Ask the instrument at an address for *IDN? and report its model and limits "
        "(max_source_v V, max_source_i A, max_power_w W). Only while idle. Once the "
        "backend knows the model, an agent's run is also held to its limits."))

    async def list_users() -> CallToolResult:
        return result(await ask(backend, 'GET', '/users'))

    server.add_tool(list_users, annotations=READ, title="List users", description=(
        "The operator profiles, and the one last used. A run is started as one of them."))

    async def get_profile(user: User) -> CallToolResult:
        return result(await ask(backend, 'GET', f'/profiles/{_segment(user)}'))

    server.add_tool(get_profile, annotations=READ, title="Get profile", description=(
        "A user's stored settings: measurement (every mode's keys, in SI units), display, "
        "file, output, and agent_limits (max_voltage_v V, max_current_a A, max_power_w W; "
        "null means only the instrument's own limit). Only a person can change "
        "agent_limits or the touch-safety keys."))

    async def describe_mode(mode: Mode,
                            user: Annotated[Optional[str], Field(
                                description="Whose stored values to show; default the "
                                            "last user.")] = None) -> CallToolResult:
        entry = await override_keys(backend, mode)
        described: Dict[str, Any] = {
            'mode': mode,
            'mode_keys': entry.get('fields', []),
            'override_keys': entry.get('override_keys', []),
        }
        user = user or (await ask(backend, 'GET', '/users')).get('last_user')
        if user:
            resolved = await ask(backend, 'POST', '/settings/resolve', json_body={
                'mode': mode, 'username': user, 'overrides': {}})
            measurement = resolved.get('settings', {}).get('measurement', {})
            described['user'] = user
            described['values'] = {key: measurement[key] for key in described['override_keys']
                                   if key in measurement}
            described['issues'] = resolved.get('issues', [])
        return result(described)

    server.add_tool(describe_mode, annotations=READ, title="Describe mode", description=(
        "The settings one mode takes: mode_keys (the mode's own), override_keys (all a "
        "run of this mode accepts in overrides), and, for a user, the value each would "
        "have if not overridden. Units are SI and follow the key name (…_voltage V, "
        "…_current A, …_compliance in the unit it limits, sampling_rate Hz, …_hours, "
        "…_s, …_cm, …_um, …_mm). Bounds are checked by check_settings, which names the "
        "key and the bound of any value out of range."))

    async def check_settings(user: User, mode: Mode,
                             overrides: Overrides = None) -> CallToolResult:
        keys = (await override_keys(backend, mode)).get('override_keys', [])
        resolved = await ask(backend, 'POST', '/settings/resolve', json_body={
            'mode': mode, 'username': user, 'overrides': overrides or {}})
        measurement = resolved.get('settings', {}).get('measurement', {})
        limits = resolved.get('agent_limits')
        hazard = resolved.get('hazard')
        checked: Dict[str, Any] = {
            'ok': resolved.get('ok'),
            'agent_may_start': bool(resolved.get('ok') and limits and limits.get('ok')),
            'issues': resolved.get('issues', []),
            'agent_limits': limits,
            'hazard': hazard,
            'derived': resolved.get('derived'),
            'settings': {key: measurement[key] for key in keys if key in measurement},
        }
        if hazard and hazard.get('hazardous'):
            checked['note'] = ("At or above the touch-safety threshold: a run an agent starts "
                               "waits at a touch-safety prompt until a person at the "
                               "ResistaMet window answers it.")
        return result(checked)

    server.add_tool(check_settings, annotations=READ, title="Check settings", description=(
        "Dry run: what a run would use, without touching the instrument. Call this before "
        "every start_run. Returns ok and the issues (key, message, severity), the "
        "resolved values of the mode's keys, derived values (max_rate_hz, sweep_points, "
        "worst_case_power_w), hazard (the touch-safety check, voltage_v against "
        "threshold_v), and agent_limits: whether an agent may start it, and each "
        "violation's limit, keys, value and allowed value. agent_may_start false means "
        "start_run will be refused. The limits (by default 30 V; current and power left "
        "to the instrument) are per profile and only a person can change them."))


def _segment(value: str) -> str:
    """A user name as one URL path segment."""
    return quote(value, safe='')
