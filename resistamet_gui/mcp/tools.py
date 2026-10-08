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
from typing import Annotated, Any, Dict, List, Optional
from urllib.parse import quote

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

from ..constants import __version__
from . import audit, summary, waiting
from . import describe as describing
from .client import Backend, BackendError, BackendUnavailable

#: Said wherever a prompt needs a person, so the agent can pass it on. The
#: wait it names carries the prompt's id, so it works whether or not this
#: server process is the one that showed the prompt (a client may start a
#: new one for every call).
PERSON_MUST_ANSWER = ("A person must answer this at the ResistaMet window; an agent "
                      "cannot. Tell the user what it asks (detail.message), then "
                      "wait_for('prompt_answered', ignore_prompt_id='{prompt_id}'): it "
                      "returns once this prompt is answered or released, or the run ends, "
                      "with the state and any next prompt; on a timeout, call it again.")

MODES = "resistance, source_v, source_i, four_point, sweep, vdp"

#: What a van der Pauw run asks of a person, as ``session/vdp_run.py`` does it:
#: the touch-safety question first when it applies, before the instrument is
#: opened, then one rewiring prompt per F76 geometry (``f76_geometries``,
#: four), each with the output off.
VDP_PROMPTS = (
    "A van der Pauw run stops at four prompts (kind vdp_geometry), one before each of "
    "its four wirings: the output is off while it waits, the prompt's detail.message "
    "says which contacts take Force HI/LO and Sense HI/LO, and a person rewires the "
    "leads and presses Measure at the ResistaMet window (the answer 'proceed'). If "
    "vdp_voltage_compliance is at or above the profile's touch-safety threshold, a "
    "touch-safety prompt (safety_voltage_ack) comes first. Each prompt waits "
    "prompt_timeout_s (900 s by default; start_run sets it), then the run ends. "
    "describe_mode('vdp') gives the four wirings in the words the prompts will use. A "
    "person must be at the bench for the whole run; an agent can start it, follow it "
    "and stop it, but not move it on.")

#: Profile keys get_profile leaves out (see ``for_an_agent``).
HIDDEN_PROFILE_KEYS = ('allow_agents',)

#: Read-only, and safe to repeat.
READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True,
                       open_world_hint=False)
#: Changes what the instrument does, but neither deletes nor overwrites:
#: every run writes a new file.
ACT = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                      open_world_hint=False)
#: wait_for: only reads, unless asked to stop the run when its condition holds.
WAIT = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                       open_world_hint=False)
#: As ACT, and asking twice is the same as asking once.
ACT_IDEMPOTENT = ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                 idempotent_hint=True, open_world_hint=False)

#: The program that asks for a run, as the data file's client.* lines name it.
CLIENT = {'name': 'resistamet-mcp', 'version': __version__}

#: The largest file the backend serves (``routes_results.MAX_PREVIEW_BYTES``).
MAX_FILE_BYTES = 32 * 1024 * 1024

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


def for_an_agent(profile: Dict[str, Any]) -> Dict[str, Any]:
    """A stored profile without the keys that would mislead an agent.

    ``allow_agents`` is this machine's stored switch, and it is not what lets
    an agent in: ``--allow-agents`` turns access on without it. An agent
    that read ``allow_agents: false`` while connected doubted it was allowed
    to act; being connected is the answer, so the key is left out.
    """
    measurement = profile.get('measurement')
    if not isinstance(measurement, dict):
        return profile
    return {**profile, 'measurement': {key: value for key, value in measurement.items()
                                       if key not in HIDDEN_PROFILE_KEYS}}


class ShownPrompt:
    """The prompt this agent was last shown pending, by id.

    ``wait_for`` waits through that prompt and returns at any other: the
    agent has passed the first on to the user and is waiting for the
    answer, but has not heard of the second. "Pending when the wait began"
    is not the same thing. A prompt raised between ``start_run``'s reply
    and the agent's first wait was pending at the start and never shown,
    and waiting through it would leave the agent silent while the run
    waits for a person nobody told.

    It lasts as long as this server process. A client that starts a new
    server for every call, or reconnects, has nothing remembered, and
    each wait at a prompt came back at once with it; ``prompt_answered``
    and ``ignore_prompt_id`` are the forms that need no memory.
    """

    def __init__(self) -> None:
        self.prompt_id: Optional[str] = None

    def note(self, status: Dict[str, Any]) -> None:
        prompt = status.get('pending_prompt')
        self.prompt_id = prompt.get('prompt_id') if isinstance(prompt, dict) else None


def status_view(status: Dict[str, Any],
                shown: Optional[ShownPrompt] = None) -> Dict[str, Any]:
    """``GET /session`` with a prompt that needs a person said to need one.

    ``shown`` notes the prompt, if any: the status is about to reach the agent.
    """
    if shown is not None:
        shown.note(status)
    view = dict(status)
    prompt = view.get('pending_prompt')
    if isinstance(prompt, dict) and prompt.get('requires_human'):
        view['pending_prompt'] = {**prompt, 'who_answers': PERSON_MUST_ANSWER.format(
            prompt_id=prompt.get('prompt_id'))}
    return view


#: check_settings' note on a run at or above the touch-safety threshold.
NOTE_HAZARD = ("At or above the touch-safety threshold: a run an agent starts waits at a "
               "touch-safety prompt until a person at the ResistaMet window answers it.")

#: check_settings' note on a resistance run in auto range. Not an API warning:
#: the window's users chose auto range knowing what it does, and the desktop
#: app greys the current and limit fields out under it. An agent reads
#: res_test_current as the current it will source. In the third trial it asked
#: for 1 mA, the instrument used 100 mA, and the file name and
#: params.test_current_A still said 1 mA.
NOTE_AUTO_RANGE = ("res_auto_range is true: the instrument's auto-ohms chooses the test "
                   "current and the voltage limit itself, per range, so neither "
                   "res_test_current nor res_voltage_compliance is what will be applied "
                   "(100 mA has been seen for a 1 mA request). The file name and "
                   "params.test_current_A still give res_test_current; the I_meas column "
                   "records the current that flowed. To source exactly res_test_current "
                   "under res_voltage_compliance, pass res_auto_range false in overrides.")


def setting_notes(mode: str, measurement: Dict[str, Any],
                  hazard: Optional[Dict[str, Any]]) -> List[str]:
    """What a run with these settings does that an agent would not guess."""
    notes = []
    if hazard and hazard.get('hazardous'):
        notes.append(NOTE_HAZARD)
    if mode == 'resistance' and measurement.get('res_auto_range'):
        notes.append(NOTE_AUTO_RANGE)
    return notes


def entry_of(schema: Dict[str, Any], mode: str) -> Dict[str, Any]:
    """One mode's entry in the schema route's reply, or a tool error naming the modes."""
    modes = schema.get('modes', {})
    if mode not in modes:
        raise ToolError(f"unknown mode '{mode}'; the modes are: {', '.join(sorted(modes))}")
    return modes[mode]


async def mode_entry(backend: Backend, mode: str) -> Dict[str, Any]:
    """The schema route's entry for one mode, or a tool error naming the modes."""
    return entry_of(await ask(backend, 'GET', '/schema/settings'), mode)


def register(server: MCPServer, backend: Backend) -> None:
    """Add every tool to ``server``."""
    shown = ShownPrompt()
    _register_reads(server, backend, shown)
    _register_runs(server, backend, shown)
    _register_following(server, backend, shown)
    _register_results(server, backend)


def _register_reads(server: MCPServer, backend: Backend, shown: ShownPrompt) -> None:

    async def get_status() -> CallToolResult:
        health = await ask(backend, 'GET', '/health')
        status = status_view(await ask(backend, 'GET', '/session'), shown)
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
        "Only while idle: a scan puts traffic on the bus. A run always talks to the "
        "instrument at the profile's gpib_address (get_profile shows it): a run's "
        "overrides cannot name an address, and no tool here changes the profile. If it "
        "is not the instrument found here, ask the user to set it at the ResistaMet "
        "window."))

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
        stored = await ask(backend, 'GET', f'/profiles/{_segment(user)}')
        return result(for_an_agent(stored))

    server.add_tool(get_profile, annotations=READ, title="Get profile", description=(
        "A user's stored settings: measurement (every mode's keys, in SI units), display, "
        "file, output, and agent_limits (max_voltage_v V, max_current_a A, max_power_w W; "
        "null means only the instrument's own limit). Only a person can change "
        "agent_limits or the touch-safety keys. The machine's agent-access switch is "
        "left out: being connected means agent access is on."))

    async def describe_mode(mode: Mode,
                            user: Annotated[Optional[str], Field(
                                description="Whose stored values to show; default the "
                                            "last user.")] = None) -> CallToolResult:
        schema = await ask(backend, 'GET', '/schema/settings')
        entry = entry_of(schema, mode)
        user = user or (await ask(backend, 'GET', '/users')).get('last_user')
        measurement = profile = None
        issues = None
        if user:
            resolved = await ask(backend, 'POST', '/settings/resolve', json_body={
                'mode': mode, 'username': user, 'overrides': {}})
            measurement = resolved.get('settings', {}).get('measurement', {})
            issues = resolved.get('issues', [])
            if entry.get('fixed'):
                stored = await ask(backend, 'GET', f'/profiles/{_segment(user)}')
                profile = stored.get('measurement', {})
        described: Dict[str, Any] = {'mode': mode, 'user': user,
                                     **describing.describe(entry, measurement, profile),
                                     'how_to_read': describing.HOW_TO_READ}
        if mode == 'vdp':
            described['prompts'] = VDP_PROMPTS
            # The backend's, as its run will raise them; absent from an
            # older backend, which this server may be talking to.
            for key, value in (('wiring', entry.get('wiring')),
                               ('prompt_timeout_s', schema.get('prompt_timeout_s'))):
                if value is not None:
                    described[key] = value
        if issues is not None:
            described['issues'] = issues
        return result(described)

    server.add_tool(describe_mode, annotations=READ, title="Describe mode", description=(
        "The settings one mode takes, one line per key: mode_keys (the mode's own) and "
        "shared_keys (timing, filter, aux sensor). Each line gives the value a user's run "
        "would have, in its unit, and where it comes from (the profile, or fixed by the "
        "mode), the default, what the key accepts (its choices, or its bounds), and what "
        "it means. A key the mode does not fix can be changed for one run in overrides. "
        "For vdp, prompts says what a person must answer during the run, and when; "
        "wiring lists the four wirings (force_hi, force_lo, sense_hi, sense_lo and the "
        "message each prompt will show), so you can tell the person before the run; "
        "prompt_timeout_s gives how long each prompt waits by default, in s, and the most "
        "start_run may ask for."))

    async def check_settings(user: User, mode: Mode,
                             overrides: Overrides = None) -> CallToolResult:
        keys = (await mode_entry(backend, mode)).get('override_keys', [])
        resolved = await ask(backend, 'POST', '/settings/resolve', json_body={
            'mode': mode, 'username': user, 'overrides': overrides or {}})
        measurement = resolved.get('settings', {}).get('measurement', {})
        limits = resolved.get('agent_limits')
        hazard = resolved.get('hazard')
        # can_start first, and the backend's ok after it: in a trial, an
        # agent read "ok": true beside a refusal as leave to start.
        checked: Dict[str, Any] = {
            'can_start': bool(resolved.get('ok') and limits and limits.get('ok')),
            'ok': resolved.get('ok'),
            'issues': resolved.get('issues', []),
            'warnings': resolved.get('warnings', []),
            'agent_limits': limits,
            'hazard': hazard,
            'derived': resolved.get('derived'),
            'settings': {key: measurement[key] for key in keys if key in measurement},
        }
        notes = setting_notes(mode, measurement, hazard)
        if notes:
            checked['notes'] = notes
        return result(checked)

    server.add_tool(check_settings, annotations=READ, title="Check settings", description=(
        "Dry run: what a run would use, without touching the instrument. Call this before "
        "every start_run. can_start: start_run would accept these settings from you (they "
        "are valid and within the agent limits); false means it will be refused. ok: the "
        "settings are valid, whatever the limits say. Then the issues (key, message, "
        "severity), warnings "
        "(keys, message: what the run will warn about once going, e.g. a sampling_rate "
        "above what the timing settings can deliver, or four-point power above "
        "fpp_power_warn_w; they never stop a start), the "
        "resolved values of the mode's keys, derived values (max_rate_hz, sweep_points, "
        "worst_case_power_w), hazard (the touch-safety check, voltage_v against "
        "threshold_v), and agent_limits: whether the limits allow it, and each "
        "violation's limit, keys, value and allowed value. The limits (by default 30 V; "
        "current and power left to the instrument) are per profile and only a person can "
        "change them. notes: what these settings mean for the run that is easy to miss, "
        "e.g. a touch-safety prompt, or a resistance run in auto range, where the "
        "instrument chooses the test current and voltage limit itself."))


def _register_runs(server: MCPServer, backend: Backend, shown: ShownPrompt) -> None:

    async def start_run(
            user: User, mode: Mode,
            sample_name: Annotated[str, Field(description="Sample name; it goes into the "
                                                          "data file's name and header.")],
            overrides: Overrides = None,
            spot: Annotated[Optional[Dict[str, Any]], Field(description=(
                "four_point only: which placement of a map this run is, {\"map_id\", "
                "\"index\", \"label\", optional \"x_mm\", \"y_mm\", \"angle_deg\"}. "
                "A spot off the sample is refused before the output turns on."))] = None,
            prompt_timeout_s: Annotated[Optional[float], Field(gt=0, description=(
                "How long a prompt may wait for a person before the run is abandoned, "
                "in seconds; default 900."))] = None,
    ) -> CallToolResult:
        body: Dict[str, Any] = {'mode': mode, 'username': user, 'sample_name': sample_name,
                                'overrides': overrides or {}, 'client': CLIENT}
        if spot is not None:
            body['spot'] = spot
        if prompt_timeout_s is not None:
            body['prompt_timeout_s'] = prompt_timeout_s
        started = await ask(backend, 'POST', '/session/start', json_body=body)
        audit.note_run_id(started.get('run_id'))
        status = status_view(await ask(backend, 'GET', '/session'), shown)
        return result({'run_id': started.get('run_id'), 'status': status})

    server.add_tool(start_run, annotations=ACT, title="Start run", description=(
        "Start a measurement as a user, with that user's profile and any overrides. "
        "Call check_settings with the same arguments first. Returns at once with the "
        "run_id and the session status; follow the run with wait_for. Refused with the "
        "backend's reasons: 422 for settings that do not resolve (each key and message) "
        "or a run beyond the agent limits (each violation: limit, keys, value, allowed; "
        "only a person can raise a limit), 409 when a run is already going or another "
        "program holds the instrument. A run at or above the profile's touch-safety "
        "threshold (30 V by default) first stops at a touch-safety prompt. A van der "
        "Pauw run stops at four more, one before each of its four wirings, with the "
        "output off, for a person to rewire the leads: someone must be at the bench for "
        "the whole run (describe_mode('vdp') says more). Only a person at the "
        "ResistaMet window can answer a prompt. The data file records started_by: "
        "agent. stop_run ends it."))

    async def stop_run() -> CallToolResult:
        return result(status_view(await ask(backend, 'POST', '/session/stop'), shown))

    server.add_tool(stop_run, annotations=ACT_IDEMPOTENT, title="Stop run", description=(
        "End the run in progress, whoever started it, the normal way: output off, data "
        "file finished with its footer (reason user_stop). Always allowed; a no-op when "
        "idle. Also releases a run waiting at a prompt. wait_for('run_ended') follows "
        "the shutdown."))

    async def abort_run() -> CallToolResult:
        return result(status_view(await ask(backend, 'POST', '/session/abort'), shown))

    server.add_tool(abort_run, annotations=ACT_IDEMPOTENT, title="Abort run", description=(
        "End the run as stop_run does (output off, file finished) but record the reason "
        "as aborted rather than user_stop. Always allowed."))

    async def pause_run() -> CallToolResult:
        return result(status_view(await ask(backend, 'POST', '/session/pause'), shown))

    server.add_tool(pause_run, annotations=ACT_IDEMPOTENT, title="Pause run", description=(
        "Pause a continuous run: sampling stops, the output stays on. No effect on a "
        "van der Pauw run. 409 when no run is in progress."))

    async def resume_run() -> CallToolResult:
        return result(status_view(await ask(backend, 'POST', '/session/resume'), shown))

    server.add_tool(resume_run, annotations=ACT_IDEMPOTENT, title="Resume run",
                    description="Resume a paused run. 409 when no run is in progress.")

    async def mark_event(
            label: Annotated[str, Field(description=(
                "One line of at most 80 characters, e.g. 'lamp on'."))] = 'MARK',
    ) -> CallToolResult:
        return result(status_view(await ask(backend, 'POST', '/session/mark',
                                            json_body={'label': label}), shown))

    server.add_tool(mark_event, annotations=ACT, title="Mark event", description=(
        "Write a label into the event column of the run's next data row, e.g. when "
        "something changed at the bench. Marks before one row are joined with '; '. "
        "409 when no run is in progress; 422 names what the backend refused in a label."))


def _register_following(server: MCPServer, backend: Backend, shown: ShownPrompt) -> None:

    async def get(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return await ask(backend, 'GET', path, params=params)

    async def wait_for(
            until: Annotated[str, Field(description=(
                "run_ended, prompt (one pending now, or raised during the wait), "
                "prompt_answered (the prompt pending now is no longer pending), "
                "samples:N (the run has written N samples) or state:<state> (idle, "
                "identifying, running, paused, awaiting_prompt, stopping)."))],
            timeout_s: Annotated[float, Field(ge=0, description=(
                f"Seconds to wait, at most {waiting.MAX_WAIT_S:g}."))] = 60.0,
            then_stop: Annotated[bool, Field(description=(
                "Stop the run as soon as the condition holds, in this call, and wait "
                "(within the same timeout) for it to end."))] = False,
            ignore_prompt_id: Annotated[Optional[str], Field(description=(
                "A prompt's prompt_id that does not end this wait: the one you have "
                "told the user about. With prompt_answered, the prompt you expect to be "
                "waiting on."))] = None,
    ) -> CallToolResult:
        try:
            condition = waiting.parse_until(until)
        except ValueError as exc:
            raise ToolError(str(exc)) from None

        async def stop() -> None:
            await ask(backend, 'POST', '/session/stop')

        waited = await waiting.wait_for(get, condition, timeout_s,
                                        stop=stop if then_stop else None,
                                        known_prompt=shown.prompt_id,
                                        ignore_prompt=ignore_prompt_id)
        waited['status'] = status_view(waited['status'], shown)
        audit.note_run_id(waited['status'].get('run_id'))
        return result(waited)

    server.add_tool(wait_for, annotations=WAIT, title="Wait for", description=(
        "Wait on the current run instead of polling get_status. Returns once, with "
        "fired = the condition asked for, or 'prompt' (a prompt you have not been shown "
        "is pending; if it requires_human, a person must answer it at the ResistaMet "
        "window: tell the user), or 'run_ended' (no run is in progress; run_ended then "
        "gives the reason, ok, samples and data file), or 'timeout'; always with the "
        "session status. To wait for a person, tell the user what the prompt asks, then "
        "wait_for('prompt_answered', ignore_prompt_id=<its prompt_id>): it returns when "
        "that prompt is answered or released, or the run ends, with the state and any "
        "next prompt; a timeout means no answer yet (prompt_at_start.still_pending). "
        "This needs nothing remembered between calls. ignore_prompt_id also lets "
        "run_ended, state: and samples: wait through that prompt (and makes prompt wait "
        "for the next one). A prompt already shown to you in this session does not end "
        "a run_ended, state: or samples: wait either, but a client that reconnects or "
        "restarts the server forgets what was shown. "
        f"The timeout is at most {waiting.MAX_WAIT_S:g} s; call again to keep waiting. "
        "then_stop true stops the run the moment the condition holds and returns once it "
        "has ended (stopped true, run_ended with the final sample count). This is how to "
        "take a fixed number of readings in a mode without a sample count (resistance, "
        "source_v, source_i; four_point has fpp_samples): start_run, then "
        "wait_for('samples:N', then_stop=true). The file then holds N rows or a few more, "
        "read while the stop was on its way (at most the readings of 0.1 s, plus one); "
        "get_run_summary with first_rows=N summarises exactly N. A prompt or a timeout "
        "does not stop the run."))

    async def get_run_events(
            since_seq: Annotated[int, Field(ge=0, description=(
                "Events after this seq of the run; 0 for all the backend still holds. "
                "Pass back the last_seq of the previous call."))] = 0,
            run_id: Annotated[Optional[str], Field(description=(
                "Which run; default the current or last."))] = None,
            types: Annotated[Optional[List[str]], Field(description=(
                "Only these event types, e.g. [\"error\", \"prompt\", \"run_ended\"]."))]
            = None,
            include_samples: bool = False,
            max_samples: Annotated[int, Field(ge=1, le=waiting.MAX_SAMPLES, description=(
                f"Samples returned at most, spread over the run; at most "
                f"{waiting.MAX_SAMPLES}."))] = waiting.MAX_SAMPLES,
    ) -> CallToolResult:
        run_id = run_id or (await get('/session')).get('run_id')
        if not run_id:
            return result({'run_id': None, 'events': [], 'last_seq': since_seq})
        events, gap, _ = await waiting.read_history(get, run_id, since_seq)
        digested = waiting.digest(events, types=types, include_samples=include_samples,
                                  max_samples=max_samples)
        if digested['last_seq'] is None:
            digested['last_seq'] = since_seq
        audit.note_run_id(run_id)
        return result({'run_id': run_id, 'gap': gap, **digested})

    server.add_tool(get_run_events, annotations=READ, title="Run events", description=(
        "What happened in a run, as the backend reported it: lifecycle, logs, errors, "
        "prompts, compliance changes, overpower trips, sweep segments and results "
        "(vdp_result, spot_complete, run_ended). Progress logs and samples are left out "
        "unless named in types; include_samples adds samples thinned to max_samples, "
        "and samples_total counts them either way. For statistics over every row use "
        "get_run_summary. gap true: the backend's memory no longer reaches back that far."))


def _register_results(server: MCPServer, backend: Backend) -> None:

    async def get(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return await ask(backend, 'GET', path, params=params)

    async def read_file(path: str) -> str:
        try:
            return await backend.get_text('/results/file', params={'path': path},
                                          max_bytes=MAX_FILE_BYTES)
        except BackendUnavailable as exc:
            raise ToolError(str(exc)) from None
        except BackendError as exc:
            if exc.status == 415:
                raise ToolError(f"{path} is not a plain .csv (a compressed .csv.gz or an HDF5 "
                                "file), and only those can be read here; it is in the data "
                                "directory.") from None
            raise ToolError(explain(exc)) from None

    async def run_file(run_id: Optional[str]):
        """The listed path of the file a run wrote, and the run's id."""
        status = await get('/session')
        run_id = run_id or status.get('run_id')
        if not run_id:
            raise ToolError("No run yet. list_results lists the data files; pass one as path.")
        if run_id == status.get('run_id'):
            # The current or last run: its status names the file, after any
            # compression at the end.
            run_path = status.get('path')
        else:
            events, _, _ = await waiting.read_history(get, run_id, 0)
            paths = [(event.get('payload') or {}).get('path') for event in events
                     if event.get('type') in ('file_opened', 'file_finalized', 'run_ended')]
            run_path = next((path for path in reversed(paths) if path), None)
            if not events:
                raise ToolError(f"The backend no longer remembers {run_id} (run ids restart "
                                "with the backend). list_results lists the data files; pass "
                                "one as path.")
        if not run_path:
            raise ToolError(f"{run_id} wrote no data file; get_run_events says how it ended.")
        listing = await get('/results', params={'limit': 5000})
        listed = summary.find_listed(run_path, listing.get('files', []))
        if listed is None:
            raise ToolError(f"{run_path} is not among the files the backend lists.")
        return listed, run_id

    async def get_run_summary(
            run_id: Annotated[Optional[str], Field(description=(
                "Which run; default the current or last."))] = None,
            path: Annotated[Optional[str], Field(description=(
                "Or a data file, as list_results gives it."))] = None,
            first_rows: Annotated[Optional[int], Field(ge=1, description=(
                "Summarise only the first N data rows, e.g. the N readings asked for "
                "when a run was stopped after N."))] = None,
    ) -> CallToolResult:
        if path is None:
            path, run_id = await run_file(run_id)
        text = await read_file(path)
        summarised = await anyio.to_thread.run_sync(summary.summarise, text, first_rows)
        audit.note_run_id(run_id)
        return result({'run_id': run_id, 'path': path, **summarised})

    server.add_tool(get_run_summary, annotations=READ, title="Run summary", description=(
        "Statistics of a run from its data file, which holds every row: per numeric "
        "column its unit, count, mean, sample SD, min, max and last value (over all rows, "
        "compliance rows included); rows in compliance, by kind; marks; the header "
        "(settings, instrument, started_by) and, once the run is over, the end block "
        "(total_samples, duration, a four-point run's spot_stats, a van der Pauw result). "
        "finalized false: the run is still writing. Works during a run, too. "
        "result: the run's headline, a unit on every number, rows in compliance left "
        "out: resistance/source modes the main quantity's mean and SD; four_point Rs "
        "(and rho, sigma) with u_stat, u_inst, u_total; vdp R_s and rho with their "
        "uncertainty and the F76 homogeneity verdict, criterion and threshold; sweep a "
        "least-squares R with its standard error, intercept, n and r2. Its headline says "
        "it in one line, and its uncertainty says what the uncertainties are (standard, "
        "k = 1). first_rows=N: statistics, compliance and marks over the first N rows "
        "only (rows_total says how many the file has). Plain .csv files only."))

    async def list_results(
            user: Annotated[Optional[str], Field(description="Only this user's files.")] = None,
            sample: Annotated[Optional[str], Field(description=(
                "Only files whose name contains this, case-insensitive."))] = None,
            limit: Annotated[int, Field(ge=1, le=500, description="At most this many.")] = 50,
    ) -> CallToolResult:
        params: Dict[str, Any] = {'limit': 5000 if sample else limit}
        if user:
            params['user'] = user
        listing = await get('/results', params=params)
        files = listing.get('files', [])
        if sample:
            files = [f for f in files if sample.lower() in str(f.get('name', '')).lower()]
        return result({'root': listing.get('root'), 'files': files[:limit]})

    server.add_tool(list_results, annotations=READ, title="List results", description=(
        "Data files under the data directory, newest first: path (relative, to pass to "
        "read_result or get_run_summary), name, user, size in bytes, modified (Unix "
        "seconds)."))

    async def read_result(
            path: Annotated[str, Field(description="A data file, as list_results gives it.")],
            offset: Annotated[int, Field(description=(
                "First row, from 0; negative counts from the end (-10: the last ten)."))] = 0,
            rows: Annotated[int, Field(ge=1, le=summary.MAX_ROWS, description=(
                f"How many rows, at most {summary.MAX_ROWS}."))] = summary.DEFAULT_ROWS,
    ) -> CallToolResult:
        text = await read_file(path)
        sliced = await anyio.to_thread.run_sync(summary.read_slice, text, offset, rows)
        return result({'path': path, **sliced})

    server.add_tool(read_result, annotations=READ, title="Read result", description=(
        "A data file's header, end block, columns and units, and a slice of its rows "
        f"(default {summary.DEFAULT_ROWS}, at most {summary.MAX_ROWS}), never the whole "
        "file; total_rows says how many there are. For statistics use get_run_summary."))

    async def list_maps(user: User) -> CallToolResult:
        return result(await ask(backend, 'GET', '/maps', params={'user': user}))

    server.add_tool(list_maps, annotations=READ, title="List maps", description=(
        "The four-point map ids a user's runs name. A map is the set of four-point runs "
        "that share a map_id, one spot each."))

    async def get_map(
            map_id: Annotated[str, Field(description="As list_maps gives it.")],
            user: User,
    ) -> CallToolResult:
        return result(await ask(backend, 'GET', f'/maps/{_segment(map_id)}',
                                params={'user': user}))

    server.add_tool(get_map, annotations=READ, title="Get map", description=(
        "A four-point map assembled from the run files now: each spot's index, label, "
        "position (x_mm, y_mm, angle_deg), data file and statistics (rs in ohm/sq, rho "
        "in ohm cm, sigma in S/cm: mean, sd, u_total), the statistics across spots, runs "
        "that could not be read (skipped), and the stored sample photo, if any."))


def _segment(value: str) -> str:
    """A user name as one URL path segment."""
    return quote(value, safe='')
