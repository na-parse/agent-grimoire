'''FastAPI application: agent API, self-describing instructions and operator UI.'''

from __future__ import annotations

import asyncio
import json
import re
import secrets
import string
import time
from collections import Counter
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import parse_qs, quote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.requests import ClientDisconnect

from .config import AGENT_RE, BODY_MAX, CHANNEL_RE, OPERATOR, Settings, load_settings
from .db import GrimoireDB
from .files import FileStore
from .instructions import NOTICE_MAX, render_instructions
from .ntfy import NtfyNotifier
from .ui import (
    FAVICON_SVG,
    render_agents,
    render_channel,
    render_channels,
    render_files,
    render_overview,
)

Priority = Literal['min', 'low', 'default', 'high', 'urgent']
MAX_WAIT = 300.0
# open waits check this often whether their client has gone
DISCONNECT_CHECK = 5.0
# a wait woken by a message lingers this long so a burst comes back together
BATCH_DELAY = 0.5
META_MAX = 2000
NAME_CHARS = string.ascii_lowercase + string.digits
LIVE = ('listening', 'active')
BAD_NAME = 'invalid agent name; register with POST /api/agents to get one'
SESSION_HINT = 'take the exact name from GET /api/agents, or leave the field out'
# Default sidebar/list cap for the operator UI
UI_AGENTS = 30
# Stale/offline sessions stay in the default agent list this long
RECENT = 24 * 3600.0
# Seconds between file garbage collection passes
FILES_GC_INTERVAL = 600.0
FILE_ID_LEN = 8
# Largest message/notice request body read (bytes); covers BODY_MAX in any encoding
REQUEST_MAX = 64 * 1024


def _slug(text: str) -> str:
    '''Lowercase ``a-z0-9-`` form of ``text`` (max 16 chars) for session names.'''
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')[:16].strip('-')


def _token(length: int) -> str:
    '''Random ``a-z0-9`` string (session name suffixes, file ids).'''
    return ''.join(secrets.choice(NAME_CHARS) for _ in range(length))


def _names(csv: str) -> list[str]:
    '''Split a comma-separated name list from a query string.'''
    return [n.strip() for n in csv.split(',') if n.strip()]


def _as_text(msgs: list[dict[str, Any]]) -> str:
    '''Messages as plain text: a ``=== #<id> from ... at ...`` line, then the body.'''
    blocks = []
    for m in msgs:
        to = f' to {",".join(m["recipients"])}' if m['recipients'] else ''
        where = f'in {m["channel"]} at {m["created_at"]}'
        blocks.append(f'=== #{m["id"]} from {m["agent"]}{to} {where}\n{m["body"]}\n')
    return '\n'.join(blocks)


def _ago(seconds: float) -> str:
    '''ISO-8601 UTC timestamp ``seconds`` in the past (matches db timestamps).'''
    then = datetime.now(UTC) - timedelta(seconds=seconds)
    return then.isoformat(timespec='seconds')


# =============================================================================
# Request Models - bodies are JSON, plain text (messages, notices) or form fields
# =============================================================================


async def _read_body(request: Request) -> tuple[str, bytes, str]:
    '''Read a capped request body: its content type, raw bytes and UTF-8 text.

    The text is empty for JSON bodies, which pydantic decodes itself.
    '''
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > REQUEST_MAX:
            raise HTTPException(413, f'request body exceeds {REQUEST_MAX} bytes')
    kind = request.headers.get('content-type', '').split(';')[0].strip().lower()
    if kind == 'application/json':
        return kind, bytes(raw), ''
    try:
        return kind, bytes(raw), raw.decode()
    except UnicodeDecodeError as exc:
        raise HTTPException(422, 'body must be UTF-8 text') from exc


def _validate[M: BaseModel](model: type[M], data: dict[str, Any] | bytes) -> M:
    '''Validate a dict or raw JSON, reporting errors like FastAPI's own.'''
    try:
        if isinstance(data, bytes):
            return model.model_validate_json(data)
        return model.model_validate(data)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors(include_url=False)) from exc


async def parse_body[M: BaseModel](
    request: Request, model: type[M], **fields: Any
) -> M:
    '''Validate a JSON body, or a plain-text ``body`` plus query ``fields``.

    ``content-type: application/json`` selects JSON; anything else (curl's default
    included) is the raw UTF-8 body text.
    '''
    kind, raw, text = await _read_body(request)
    if kind == 'application/json':
        return _validate(model, raw)
    given = {k: v for k, v in fields.items() if v is not None}
    return _validate(model, {**given, 'body': text.strip()})


async def parse_fields[M: BaseModel](request: Request, model: type[M]) -> M:
    '''Validate a JSON body, or form fields plus query parameters.

    Form bodies (``curl --data-urlencode 'status=...'``) avoid JSON quoting in the
    shell; ``meta`` is then a JSON object string. A body that looks like JSON is
    read as JSON whatever its content type (``curl -d '{...}'`` sends a form type).
    Unknown body keys are rejected with the allowed list, so a typo never silently
    does nothing; query parameters that are not fields (``?agent=``) are ignored.
    '''
    kind, raw, text = await _read_body(request)
    fields = model.model_fields
    query = {k: v for k, v in request.query_params.items() if k in fields}
    if kind == 'application/json' or text.lstrip().startswith('{'):
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(422, f'body is not valid JSON: {exc}') from exc
        if not isinstance(body, dict):
            raise HTTPException(422, 'JSON body must be an object')
    else:
        body = {k: v[-1] for k, v in parse_qs(text, keep_blank_values=True).items()}
        if 'meta' in body:
            try:
                body['meta'] = json.loads(body['meta'])
            except json.JSONDecodeError as exc:
                raise HTTPException(422, 'meta must be a JSON object') from exc
    unknown = sorted(set(body) - set(fields))
    if unknown:
        allowed = ', '.join(n for n, f in fields.items() if not f.deprecated)
        raise HTTPException(422, f'unknown fields {unknown}; allowed: {allowed}')
    return _validate(model, {**query, **body})



def _check_agent(name: str) -> str:
    '''Validate a session name format (``operator`` can never match).'''
    if not AGENT_RE.match(name):
        raise ValueError(BAD_NAME)
    return name


def _check_meta(meta: dict[str, Any] | None) -> dict[str, Any] | None:
    '''Cap the size of free-form agent metadata.'''
    if meta is not None and len(json.dumps(meta)) > META_MAX:
        raise ValueError(f'meta too large (max {META_MAX} chars as JSON)')
    return meta


# Unknown fields are rejected so a typo never silently does nothing. ``mode`` is
# accepted and ignored: older guides had agents send it.
STRICT = ConfigDict(extra='forbid')
LEGACY_MODE = Field(default=None, deprecated='presence modes were removed; ignored')


class PostIn(BaseModel):
    '''Body for ``POST /api/messages``.'''

    model_config = STRICT

    agent: str
    channel: str
    body: str = Field(min_length=1, max_length=BODY_MAX)
    to: list[str] = Field(default_factory=list)

    _agent = field_validator('agent')(_check_agent)

    @field_validator('channel')
    @classmethod
    def _channel(cls, v: str) -> str:
        if not CHANNEL_RE.match(v):
            raise ValueError('invalid channel name; use a-z0-9_.- (max 64)')
        return v

    @field_validator('to')
    @classmethod
    def _to(cls, v: list[str]) -> list[str]:
        if OPERATOR in v:
            raise ValueError(
                f'{OPERATOR} (the human) cannot be addressed; use POST /api/notify'
            )
        bad = [n for n in v if not AGENT_RE.match(n)]
        if bad:
            raise ValueError(f'invalid recipient names: {bad}')
        return v


class NoticeIn(BaseModel):
    '''Body for ``POST /api/notify``.'''

    model_config = STRICT

    agent: str
    body: str = Field(min_length=1, max_length=NOTICE_MAX)
    priority: Priority = 'default'

    _agent = field_validator('agent')(_check_agent)


class ResolveIn(BaseModel):
    '''Body for ``POST /api/notify/{id}/resolve``.'''

    model_config = STRICT

    agent: str
    note: str = Field(default='', max_length=500)

    _agent = field_validator('agent')(_check_agent)


class RegisterIn(BaseModel):
    '''Body for ``POST /api/agents`` (start a session).'''

    model_config = STRICT

    project: str = Field(min_length=1, max_length=200)
    host: str = Field(min_length=1, max_length=200)
    tool: str = Field(default='', max_length=100)
    status: str = Field(default='', max_length=200)
    meta: dict[str, Any] = Field(default_factory=dict)
    # optional join post: `channel` posts "<name> joined: <status>", addressed to
    # `lead` if given; `replaces` names a dropped session this one takes over
    channel: str | None = None
    lead: str | None = None
    replaces: str | None = None
    mode: str | None = LEGACY_MODE

    _meta = field_validator('meta')(_check_meta)

    @field_validator('channel')
    @classmethod
    def _channel(cls, v: str | None) -> str | None:
        if v is not None and not CHANNEL_RE.match(v):
            raise ValueError('invalid channel name; use a-z0-9_.- (max 64)')
        return v

    @field_validator('lead', 'replaces')
    @classmethod
    def _session(cls, v: str | None) -> str | None:
        if v == OPERATOR:
            raise ValueError(
                f'{OPERATOR} (the human) cannot be addressed; use POST /api/notify'
            )
        if v is not None and not AGENT_RE.match(v):
            raise ValueError(f'no session named {v!r}; {SESSION_HINT}')
        return v

    @field_validator('project', 'host')
    @classmethod
    def _sluggable(cls, v: str) -> str:
        if not _slug(v):
            raise ValueError('must contain at least one letter or digit')
        return v


class AgentUpdateIn(BaseModel):
    '''Body for ``POST /api/agents/{name}`` (omitted fields are unchanged).'''

    model_config = STRICT

    status: str | None = Field(default=None, max_length=200)
    meta: dict[str, Any] | None = None
    mode: str | None = LEGACY_MODE

    _meta = field_validator('meta')(_check_meta)


# =============================================================================
# Change Notification - wakes long-polling /api/wait callers
# =============================================================================


class Notifier:
    '''Version counter + condition so waiters wake on any new message.

    Also tracks which agents are blocked in ``/api/wait`` right now, which is
    the live ``listening`` presence state (in memory only, never stored).
    '''

    def __init__(self) -> None:
        self.version = 0
        self.closing = False
        self.listening: Counter[str] = Counter()
        self._cond = asyncio.Condition()

    @contextmanager
    def listen(self, agent: str) -> Iterator[None]:
        '''Mark ``agent`` as listening for the duration of a long-poll.'''
        self.listening[agent] += 1
        try:
            yield
        finally:
            self.listening[agent] -= 1
            if self.listening[agent] <= 0:
                del self.listening[agent]

    async def bump(self) -> None:
        '''Signal that new messages exist.'''
        async with self._cond:
            self.version += 1
            self._cond.notify_all()

    async def close(self) -> None:
        '''Server shutting down: wake every waiter so it can tell its client.'''
        async with self._cond:
            self.closing = True
            self._cond.notify_all()

    async def wait_change(self, seen: int, timeout: float) -> None:
        '''Wait until the version differs from ``seen``, shutdown, or the timeout.'''
        async with self._cond:
            try:
                await asyncio.wait_for(
                    self._cond.wait_for(
                        lambda: self.version != seen or self.closing
                    ),
                    timeout,
                )
            except TimeoutError:
                pass


# =============================================================================
# Application Factory
# =============================================================================


def create_app(settings: Settings | None = None) -> FastAPI:
    '''Build the Grimoire application.'''
    settings = settings or load_settings()
    db = GrimoireDB(settings.db_path)
    notifier = Notifier()
    ntfy = NtfyNotifier(settings.ntfy, settings.public_url)
    presence = settings.presence
    limits = settings.files
    store = FileStore(settings.files_dir)

    async def post(agent: str, **fields: Any) -> dict[str, Any]:
        '''Shared post path: persist and wake long-polling waiters.'''
        msg = db.add_message(agent, **fields)
        await notifier.bump()
        return msg

    def collect_files() -> None:
        '''Delete expired file records, then blobs no record references.'''
        db.expire_files()
        store.remove_orphans(db.stored_hashes())

    async def files_gc() -> None:
        '''Background garbage collection loop (runs for the app's lifetime).'''
        while True:
            await asyncio.sleep(FILES_GC_INTERVAL)
            collect_files()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # nothing is uploading yet: anything staged was left by a crash
        db.clear_uploads()
        store.clear_staging()
        collect_files()
        gc = asyncio.create_task(files_gc())
        app.state.loop = asyncio.get_running_loop()
        yield
        gc.cancel()
        await ntfy.aclose()
        db.close()

    app = FastAPI(title=settings.title, lifespan=lifespan, docs_url='/api/docs')
    app.state.db = db
    app.state.ntfy = ntfy
    app.state.collect_files = collect_files
    app.state.close_waits = notifier.close

    # -------------------------------------------------------------------------
    # Presence Helpers
    # -------------------------------------------------------------------------

    def require_agent(name: str) -> str:
        '''Validate a registered session name and record presence (any call).'''
        if not AGENT_RE.match(name):
            raise HTTPException(422, BAD_NAME)
        if db.get_agent(name) is None:
            raise HTTPException(
                404, f'unknown agent {name!r}; register with POST /api/agents'
            )
        db.touch_agent(name)
        return name

    def new_name(project: str, host: str) -> str:
        '''Generate an unused ``<project>_<host>_<rand4>`` session name.'''
        prefix = f'{_slug(project)}_{_slug(host)}_'
        while True:
            name = prefix + _token(4)
            if db.get_agent(name) is None:
                return name

    def new_file_id() -> str:
        '''Generate an unused file id.'''
        while True:
            file_id = _token(FILE_ID_LEN)
            if not db.file_exists(file_id):
                return file_id

    def show_file(record: dict[str, Any]) -> dict[str, Any]:
        '''File record with its download URL.'''
        return {**record, 'url': f'{settings.public_url}/api/files/{record["id"]}'}

    def state(agent: dict[str, Any]) -> str:
        '''Presence state: offline, listening, active or stale.'''
        if agent['offline_at']:
            return 'offline'
        if notifier.listening[agent['name']]:
            return 'listening'
        return 'active' if agent['last_seen'] >= _ago(presence.timeout) else 'stale'

    def show(agent: dict[str, Any]) -> dict[str, Any]:
        '''Agent record with its presence state and ``idle`` seconds since a call.'''
        seen = datetime.fromisoformat(agent['last_seen'])
        idle = max(0, int((datetime.now(UTC) - seen).total_seconds()))
        return {**agent, 'state': state(agent), 'idle': idle}

    def agents_view(everyone: bool = False) -> list[dict[str, Any]]:
        '''Live sessions plus recently departed ones (``everyone``: all rows).'''
        recent = _ago(RECENT)
        shown = [show(a) for a in db.list_agents()]
        if everyone:
            return shown
        return [a for a in shown if a['state'] in LIVE or a['last_seen'] >= recent]

    def recipient_warnings(to: list[str]) -> list[str]:
        '''Reject unknown recipients; warn about ones that are not live.'''
        found = {n: db.get_agent(n) for n in to}
        unknown = [n for n, a in found.items() if a is None]
        if unknown:
            raise HTTPException(422, f'unknown recipients: {unknown}')
        states = {n: state(a) for n, a in found.items() if a is not None}
        return [f'{n} is {s}' for n, s in states.items() if s not in LIVE]

    def wants_text(request: Request) -> bool:
        '''True if the client asked for plain text (``accept: text/plain``).'''
        return 'text/plain' in request.headers.get('accept', '')

    def reply(request: Request, msgs: list[dict[str, Any]]) -> Any:
        '''Messages as JSON, or as plain text when the client asks for it.'''
        return PlainTextResponse(_as_text(msgs)) if wants_text(request) else msgs

    def require(msg: dict[str, Any] | None, message_id: int) -> dict[str, Any]:
        '''Return the message or raise 404.'''
        if msg is None:
            raise HTTPException(404, f'message {message_id} not found')
        return msg

    # -------------------------------------------------------------------------
    # Instructions & Health
    # -------------------------------------------------------------------------

    @app.get('/', response_class=PlainTextResponse)
    @app.get('/llms.txt', response_class=PlainTextResponse, include_in_schema=False)
    async def instructions() -> str:
        '''Agent usage guide (markdown).'''
        return render_instructions(settings, db.list_channels())

    @app.get('/healthz')
    async def healthz() -> dict[str, str]:
        return {'status': 'ok'}

    # -------------------------------------------------------------------------
    # Agent API
    # -------------------------------------------------------------------------

    @app.post('/api/messages', status_code=201)
    async def create_message(
        request: Request,
        agent: str | None = None,
        channel: str | None = None,
        to: str = '',
    ) -> dict[str, Any]:
        '''Post a message to a channel (created on first post).

        Body: plain text with ``agent``, ``channel`` and ``to`` (comma-separated) in
        the query, or a JSON ``PostIn``. ``warnings`` is added when a recipient is
        stale or offline.
        '''
        p = await parse_body(
            request, PostIn, agent=agent, channel=channel, to=_names(to) or None
        )
        require_agent(p.agent)
        warnings = recipient_warnings(p.to)
        msg = await post(p.agent, channel=p.channel, body=p.body, recipients=p.to)
        return {**msg, 'warnings': warnings} if warnings else msg

    @app.get('/api/messages')
    async def list_messages(
        request: Request,
        channel: str | None = None,
        since: int | None = None,
        limit: int = Query(50, ge=1, le=500),
    ) -> Any:
        '''List messages (JSON, or plain text), optionally by channel and/or id.'''
        msgs = db.list_messages(channel=channel, since=since, limit=limit)
        return reply(request, msgs)

    @app.get('/api/messages/{message_id}')
    async def get_message(request: Request, message_id: int) -> Any:
        '''Fetch one message.'''
        msg = require(db.get_message(message_id), message_id)
        return PlainTextResponse(_as_text([msg])) if wants_text(request) else msg

    @app.get('/api/channels')
    async def list_channels() -> list[dict[str, Any]]:
        '''Channels with activity stats, most recently active first.'''
        return db.list_channels()

    # -------------------------------------------------------------------------
    # Agent Sessions & Presence
    # -------------------------------------------------------------------------

    @app.get('/api/agents')
    async def list_agents(
        everyone: bool = Query(False, alias='all'),
    ) -> list[dict[str, Any]]:
        '''Live and recently departed sessions, most recently seen first.'''
        return agents_view(everyone)

    @app.post('/api/agents', status_code=201)
    async def register_agent(request: Request) -> dict[str, Any]:
        '''Start a session; the returned ``name`` is the ``agent`` for all calls.

        Body: JSON ``RegisterIn`` or the same fields as form data.
        '''
        r = await parse_fields(request, RegisterIn)
        if (r.lead or r.replaces) and not r.channel:
            raise HTTPException(422, 'lead and replaces need a channel to post the join')
        for field, value in (('lead', r.lead), ('replaces', r.replaces)):
            if value and db.get_agent(value) is None:
                raise HTTPException(
                    422, f'{field}: no session named {value!r}; {SESSION_HINT}'
                )
        to = [r.lead] if r.lead else []
        warnings = recipient_warnings(to)
        db.prune_agents(_ago(presence.retention_days * 86400))
        name = new_name(r.project, r.host)
        agent = show(
            db.register_agent(name, r.project, r.host, r.tool, r.status, r.meta)
        )
        if r.channel:
            body = f'{name} joined: {r.status}' if r.status else f'{name} joined'
            if r.replaces and r.replaces not in body:
                body += f' (replacing {r.replaces})'
            joined = await post(name, channel=r.channel, body=body, recipients=to)
            agent['joined'] = {**joined, **({'warnings': warnings} if warnings else {})}
        return agent

    @app.post('/api/agents/{name}')
    async def update_agent(name: str, request: Request) -> dict[str, Any]:
        '''Update a session's status line and/or metadata (JSON or form fields).'''
        require_agent(name)
        u = await parse_fields(request, AgentUpdateIn)
        return show(db.update_agent(name, u.status, u.meta))

    @app.get('/api/agents/{name}')
    async def get_agent(name: str) -> dict[str, Any]:
        '''One session with its presence (looking it up does not touch it).'''
        agent = db.get_agent(name)
        if agent is None:
            raise HTTPException(404, f'unknown agent {name}')
        return show(agent)

    @app.delete('/api/agents/{name}')
    async def checkout_agent(name: str) -> dict[str, Any]:
        '''Check out (optional): the session shows as offline until pruned.'''
        require_agent(name)
        return show(db.checkout_agent(name))

    @app.get('/api/inbox')
    async def inbox(request: Request, agent: str, peek: bool = False) -> Any:
        '''Unread messages addressed to ``agent`` (marked read unless ``peek``).'''
        return reply(request, db.inbox(require_agent(agent), peek=peek))

    @app.get('/api/wait')
    async def wait(
        request: Request,
        agent: str,
        timeout: float = Query(60, ge=0, le=MAX_WAIT),
        peek: bool = False,
    ) -> Any:
        '''Long-poll the inbox until a message arrives or ``timeout`` elapses.

        A client that hangs up (e.g. its command was killed) is noticed within
        ``DISCONNECT_CHECK`` seconds: its messages stay unread and it stops showing
        as listening.
        '''
        require_agent(agent)
        deadline = time.monotonic() + timeout
        gone = woke = False
        try:
            with notifier.listen(agent):
                while True:
                    if await request.is_disconnected():
                        gone = True
                        return Response(status_code=499)
                    if notifier.closing:
                        # messages stay unread for the next call
                        raise HTTPException(503, 'server restarting; call again shortly')
                    seen = notifier.version
                    pending = db.inbox(agent, peek=True)
                    remaining = deadline - time.monotonic()
                    if pending and woke:
                        await asyncio.sleep(BATCH_DELAY)
                        if await request.is_disconnected():
                            gone = True
                            return Response(status_code=499)
                    if pending or remaining <= 0:
                        return reply(request, db.inbox(agent, peek=peek))
                    await notifier.wait_change(seen, min(remaining, DISCONNECT_CHECK))
                    woke = notifier.version != seen
        finally:
            if not gone:
                # a check-out made while this wait was open must stick
                db.touch_agent(agent, revive=False)

    # -------------------------------------------------------------------------
    # Files
    # -------------------------------------------------------------------------

    @app.post('/api/files', status_code=201)
    async def upload_file(
        request: Request, agent: str, name: str = Query(min_length=1, max_length=200)
    ) -> dict[str, Any]:
        '''Upload a file as the raw request body (``curl -T <path> -X POST``).

        The ``content-length`` header is the declared size, checked against the
        size cap, the optional quota and the free-space floor before any byte is
        read. Identical content is stored once.
        '''
        require_agent(agent)
        name = name.replace('\\', '/').rsplit('/', 1)[-1].strip()
        if not name:
            raise HTTPException(422, 'name must be a file name, not a path')
        try:
            declared = int(request.headers['content-length'])
        except (KeyError, ValueError) as exc:
            raise HTTPException(
                411,
                'content-length header required: upload a file with '
                '`curl -T <path> -X POST`, or pipe output into `--data-binary @-`',
            ) from exc
        if declared <= 0:
            raise HTTPException(422, 'file is empty')
        if declared > limits.max_size:
            raise HTTPException(413, f'file exceeds {limits.max_size} bytes')
        over_quota = limits.max_total and db.files_usage() + declared > limits.max_total
        if over_quota or not store.has_room(declared):
            raise HTTPException(507, 'file store is full; try again later')
        file_id = new_file_id()
        db.add_upload(file_id, agent, name, declared)
        try:
            size, sha256 = await store.receive(request.stream(), file_id, declared)
        except BaseException as exc:
            db.drop_file(file_id)
            if isinstance(exc, OverflowError):
                raise HTTPException(413, 'body is larger than content-length') from exc
            if isinstance(exc, ClientDisconnect):
                # cancelled upload: cleaned up above, nothing to log
                raise HTTPException(400, 'upload interrupted') from exc
            raise
        store.commit(file_id, sha256)
        return show_file(db.store_file(file_id, sha256, size, limits.ttl(size)))

    @app.get('/api/files')
    async def list_files(
        limit: int = Query(50, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        '''Stored files, newest first.'''
        return [show_file(f) for f in db.list_files(limit=limit)]

    def find_file(file_id: str) -> dict[str, Any]:
        '''Stored, unexpired file record; 410 if withdrawn, else 404.'''
        file_id = file_id.removeprefix('file:')  # pasted straight from a message
        record = db.get_file(file_id)
        if record is not None:
            return record
        gone = db.get_withdrawn(file_id)
        if gone is not None:
            raise HTTPException(
                410,
                f'file {file_id} ({gone["name"]}) was withdrawn by its uploader '
                f'{gone["agent"]}; any replacement comes from them',
            )
        raise HTTPException(404, f'file {file_id} not found (it may have expired)')

    @app.get('/api/files/{file_id}/info')
    async def file_info(file_id: str) -> dict[str, Any]:
        '''File details (name, size, sha256, uploader, expiry) without the content.'''
        return show_file(find_file(file_id))

    @app.delete('/api/files/{file_id}')
    async def delete_file(file_id: str, agent: str) -> dict[str, Any]:
        '''Withdraw an upload (uploader only); returns the deleted record.'''
        require_agent(agent)
        record = find_file(file_id)
        if record['agent'] != agent:
            raise HTTPException(403, f'only {record["agent"]} can delete this file')
        db.withdraw_file(record['id'])
        if record['sha256'] not in db.stored_hashes():
            store.blob_path(record['sha256']).unlink(missing_ok=True)
        return {**record, 'status': 'withdrawn'}

    @app.get('/api/files/{file_id}')
    async def download_file(file_id: str) -> FileResponse:
        '''Download a file under its original name.'''
        record = find_file(file_id)
        return FileResponse(
            store.blob_path(record['sha256']),
            filename=record['name'],
            media_type='application/octet-stream',
            headers={'x-sha256': record['sha256']},
        )

    # -------------------------------------------------------------------------
    # Operator Notices
    # -------------------------------------------------------------------------

    @app.post('/api/notify', status_code=201)
    async def create_notice(
        request: Request, agent: str | None = None, priority: str | None = None
    ) -> dict[str, Any]:
        '''Raise a notice for the operator (pushed to ntfy when configured).

        Body: plain text with ``agent`` and ``priority`` in the query, or a JSON
        ``NoticeIn``.
        '''
        n = await parse_body(request, NoticeIn, agent=agent, priority=priority)
        require_agent(n.agent)
        notice = db.add_notice(n.agent, n.body, n.priority)
        ntfy.publish_later(notice)
        return notice

    @app.get('/api/notify')
    async def list_notices(
        open_only: bool = Query(False, alias='open'),
        limit: int = Query(50, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        '''Notices: open first (oldest first), then resolved (newest first).'''
        return db.list_notices(open_only=open_only, limit=limit)

    @app.post('/api/notify/{notice_id}/resolve')
    async def resolve_notice(notice_id: int, request: Request) -> dict[str, Any]:
        '''Mark a notice handled, with an optional short note (JSON or form fields).'''
        r = await parse_fields(request, ResolveIn)
        require_agent(r.agent)
        try:
            return db.resolve_notice(notice_id, r.agent, r.note)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    # -------------------------------------------------------------------------
    # Operator UI
    # -------------------------------------------------------------------------

    def open_notices() -> int:
        '''Open notice count, shown on every operator page.'''
        return len(db.list_notices(open_only=True, limit=100))

    def storage() -> tuple[int, int]:
        '''Bytes Grimoire holds and the most it may hold (quota, else disk).'''
        used = db.files_usage()
        return used, limits.max_total or used + store.free()

    def channel_page(
        channel: str, flash: str = '', draft: dict[str, str] | None = None
    ) -> str:
        '''Render one channel (``draft`` refills the compose form after an error).'''
        channels = db.list_channels()
        known = {a['name']: a for a in agents_view(everyone=True)}
        posters = [
            n for n, act in db.agent_activity().items() if channel in act['channels']
        ]
        return render_channel(
            settings.title,
            next((c for c in channels if c['name'] == channel), None),
            channel,
            db.list_messages(channel=channel, limit=100),
            channels,
            [known.get(n, {'name': n, 'state': 'offline'}) for n in posters],
            open_notices(),
            flash=flash,
            draft=draft,
        )

    @app.get('/favicon.svg', include_in_schema=False)
    @app.get('/favicon.ico', include_in_schema=False)
    async def favicon() -> Response:
        # SVG served for .ico too: browsers probe that path before reading the page
        return Response(
            FAVICON_SVG, media_type='image/svg+xml',
            headers={'cache-control': 'public, max-age=86400'},
        )

    @app.get('/ui', response_class=HTMLResponse, include_in_schema=False)
    async def ui_index(
        channel: str | None = None, agents: str = '', flash: str = ''
    ) -> Response:
        # pre-tab links (?channel=, ?agents=all) moved to their own pages
        if channel:
            flashed = f'?flash={quote(flash)}' if flash else ''
            return RedirectResponse(f'/ui/channels/{quote(channel)}{flashed}')
        if agents == 'all':
            return RedirectResponse('/ui/agents?all=1')
        used, capacity = storage()
        return HTMLResponse(render_overview(
            settings.title,
            db.list_notices(limit=100),
            db.list_channels(),
            agents_view(),
            db.list_messages(limit=50),
            len(db.list_files(limit=500)),
            used,
            capacity,
        ))

    @app.get('/ui/agents', response_class=HTMLResponse, include_in_schema=False)
    async def ui_agents(all: int = 0) -> str:
        shown = agents_view(bool(all))
        return render_agents(
            settings.title,
            shown if all else shown[:UI_AGENTS],
            db.agent_activity(),
            db.list_channels(),
            open_notices(),
            everyone=bool(all),
            timeout_min=int(presence.timeout // 60),
        )

    @app.get('/ui/channels', response_class=HTMLResponse, include_in_schema=False)
    async def ui_channels(sort: str = 'recent', previews: int = 1) -> str:
        return render_channels(
            settings.title,
            db.list_channels(),
            db.channel_previews() if previews else {},
            open_notices(),
            sort=sort,
            show_previews=bool(previews),
        )

    @app.get(
        '/ui/channels/{channel}', response_class=HTMLResponse, include_in_schema=False
    )
    async def ui_channel(channel: str, flash: str = '') -> str:
        return channel_page(channel, flash)

    @app.get('/ui/files', response_class=HTMLResponse, include_in_schema=False)
    async def ui_files(sort: str = 'newest') -> str:
        used, capacity = storage()
        return render_files(
            settings.title,
            db.list_files(limit=500),
            used,
            capacity,
            bool(limits.max_total),
            open_notices(),
            sort=sort,
        )

    @app.post('/ui/post', include_in_schema=False)
    async def ui_post(request: Request) -> Response:
        raw = parse_qs((await request.body()).decode())
        form = {k: v[0].strip() for k, v in raw.items()}
        body = form.get('body', '')
        channel = form.get('channel', '')
        to = [n.strip() for n in form.get('to', '').split(',') if n.strip()]
        try:
            if not body or not CHANNEL_RE.match(channel):
                raise HTTPException(422, 'message body and a valid channel are required')
            if len(body) > BODY_MAX:
                raise HTTPException(422, f'message body exceeds {BODY_MAX} characters')
            warnings = recipient_warnings(to)
        except HTTPException as exc:
            # re-render with the error and the operator's draft instead of raw JSON
            shown = channel if CHANNEL_RE.match(channel) else 'general'
            page = channel_page(shown, flash=str(exc.detail), draft=form)
            return HTMLResponse(page, status_code=422)
        msg = await post(OPERATOR, channel=channel, body=body, recipients=to)
        flash = f'?flash={quote("; ".join(warnings))}' if warnings else ''
        return RedirectResponse(
            f'/ui/channels/{quote(channel)}{flash}#m{msg["id"]}', status_code=303
        )

    return app
