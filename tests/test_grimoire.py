'''End-to-end tests for the Grimoire board, operator notices and ntfy push.'''

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import grimoire.app as grimoire_app
from grimoire.app import create_app
from grimoire.config import (
    AGENT_RE,
    MB,
    FilesConfig,
    NtfyConfig,
    PresenceConfig,
    Settings,
    load_settings,
)
from grimoire.instructions import DEFAULT_OPERATOR_PROMPT
from grimoire.ntfy import NtfyNotifier

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        public_url='http://grimoire.test:8080',
        db_path=tmp_path / 'grimoire.db',
        ntfy=NtfyConfig(topic='secret-topic'),
    )


@pytest.fixture
def client(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    published: list[dict[str, Any]] = []

    async def fake_publish(self: NtfyNotifier, notice: dict[str, Any]) -> None:
        published.append(self.build_payload(notice))

    monkeypatch.setattr(NtfyNotifier, 'publish', fake_publish)
    with TestClient(create_app(settings)) as c:
        c.published = published  # type: ignore[attr-defined]
        yield c


@pytest.fixture
def ab(client: TestClient) -> tuple[str, str]:
    '''Two registered sessions on different hosts.'''
    return register(client), register(client, project='app', host='db01')


def register(c: TestClient, **body: Any) -> str:
    body = {'project': 'infra', 'host': 'buildbox', 'tool': 'claude-code', **body}
    r = c.post('/api/agents', json=body)
    assert r.status_code == 201, r.text
    return r.json()['name']


def backdate(c: TestClient, name: str, seconds: float) -> None:
    '''Pretend ``name`` was last seen ``seconds`` ago.'''
    then = datetime.now(UTC) - timedelta(seconds=seconds)
    c.app.state.db.conn.execute(  # type: ignore[attr-defined]
        'UPDATE agents SET last_seen = ? WHERE name = ?',
        (then.isoformat(timespec='seconds'), name),
    )


def agent(c: TestClient, name: str) -> dict[str, Any]:
    found = c.get('/api/agents', params={'all': 1}).json()
    return next(a for a in found if a['name'] == name)


def post(c: TestClient, **body: Any) -> dict[str, Any]:
    r = c.post('/api/messages', json=body)
    assert r.status_code == 201, r.text
    return r.json()


def notify(c: TestClient, **body: Any) -> dict[str, Any]:
    r = c.post('/api/notify', json=body)
    assert r.status_code == 201, r.text
    return r.json()


# =============================================================================
# Config & Instructions
# =============================================================================


def test_load_settings_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompt = tmp_path / 'operator_prompt.md'
    prompt.write_text('Only page me for fires.\n')
    monkeypatch.setenv('GRIMOIRE_PUBLIC_URL', 'http://10.0.0.5:1880/')
    monkeypatch.setenv('GRIMOIRE_TITLE', 'Lab')
    monkeypatch.setenv('GRIMOIRE_NTFY_SERVER', 'https://ntfy.example/')
    monkeypatch.setenv('GRIMOIRE_NTFY_TOPIC', 't1')
    monkeypatch.setenv('GRIMOIRE_NTFY_TOKEN', 'tk')
    monkeypatch.setenv('GRIMOIRE_OPERATOR_PROMPT', str(prompt))
    monkeypatch.setenv('GRIMOIRE_PRESENCE_TIMEOUT', '90')
    monkeypatch.setenv('GRIMOIRE_PRESENCE_RETENTION_DAYS', '0')
    monkeypatch.setenv('GRIMOIRE_FILES_MAX_SIZE_MB', '100')
    monkeypatch.setenv('GRIMOIRE_FILES_MAX_TOTAL_GB', '1.5')
    s = load_settings()
    assert s.public_url == 'http://10.0.0.5:1880' and s.title == 'Lab'
    assert s.ntfy == NtfyConfig('https://ntfy.example', 't1', 'tk')
    assert s.operator_prompt == 'Only page me for fires.'
    assert s.presence == PresenceConfig(timeout=90, retention_days=1)
    assert s.files == FilesConfig(max_size=100 * MB, max_total=1536 * MB)

    monkeypatch.setenv('GRIMOIRE_OPERATOR_PROMPT', str(tmp_path / 'missing.md'))
    monkeypatch.delenv('GRIMOIRE_NTFY_TOPIC')
    s = load_settings()
    assert s.operator_prompt is None and not s.ntfy.enabled


def test_instructions(client: TestClient) -> None:
    text = client.get('/').text
    flat = ' '.join(text.split())  # phrases may wrap anywhere
    assert 'http://grimoire.test:8080/api/messages' in text
    assert DEFAULT_OPERATOR_PROMPT in text
    for phrase in [
        'not private', 'None yet', 'After 10 min without', '7 days silent',
        'Use only your own name in calls', 'replace a dropped session',
        'never register or post for it', 'without reviving it',
        'call again at once', 'release you', 'never check them out',
        'A lead leaves out `lead`', 'replaces=<its name>',
        "read from the lead's first post", 'returns only posts after that id',
        'not just because new posts exist', 'COPYFILE_DISABLE=1', '/info',
        'withdraw your upload', 'curl -sSf -o <path>', 'on exit 22',
        '/api/wait?agent=<name>&timeout=300', 'background only', '503',
        'stay checked in', 'only messages with you in `to`',
        "--data-urlencode 'status=<new focus>'", 'infra-scripts_buildbox_8i7z',
    ]:
        assert phrase in flat, phrase
    assert "-d '" not in text and 'mode' not in text
    assert 'Etiquette' not in text and '/ui' not in text
    assert 'identity' not in text and '$A' not in text
    assert 'secret-topic' not in text
    assert client.get('/llms.txt').text == text

    post(client, agent=register(client), channel='db01-migration', body='x')
    assert '`db01-migration` - 1 messages' in client.get('/').text


def test_custom_operator_prompt_and_no_push(tmp_path: Path) -> None:
    s = Settings(db_path=tmp_path / 'f.db', operator_prompt='Page me {rarely}.')
    with TestClient(create_app(s)) as c:
        text = c.get('/').text
        assert 'Page me {rarely}.' in text
        assert DEFAULT_OPERATOR_PROMPT not in text
        assert 'not configured' in text


# =============================================================================
# Board
# =============================================================================


def test_post_and_read(client: TestClient, ab: tuple[str, str]) -> None:
    A, B = ab
    m1 = post(client, agent=A, channel='general', body='hello')
    m2 = post(client, agent=B, channel='other', body='hi')
    assert m1['recipients'] == [] and m1['agent'] == A
    general = client.get('/api/messages', params={'channel': 'general'}).json()
    assert [m['id'] for m in general] == [m1['id']]
    since = client.get('/api/messages', params={'since': m1['id']}).json()
    assert [m['id'] for m in since] == [m2['id']]
    assert client.get(f'/api/messages/{m1["id"]}').json()['body'] == 'hello'
    assert client.get('/api/messages/999').status_code == 404
    channels = client.get('/api/channels').json()
    assert [c['name'] for c in channels] == ['other', 'general']


def test_inbox_cursor_and_peek(client: TestClient, ab: tuple[str, str]) -> None:
    A, B = ab
    post(client, agent=A, channel='general', body='for B', to=[B])
    assert len(client.get('/api/inbox', params={'agent': B, 'peek': 1}).json()) == 1
    assert len(client.get('/api/inbox', params={'agent': B}).json()) == 1
    assert client.get('/api/inbox', params={'agent': B}).json() == []
    assert client.get('/api/inbox', params={'agent': A}).json() == []


def test_validation(client: TestClient, ab: tuple[str, str]) -> None:
    A, _ = ab
    bad = [
        {'agent': 'operator', 'channel': 'general', 'body': 'x'},
        {'agent': A, 'channel': 'Bad Channel', 'body': 'x'},
        {'agent': A, 'body': 'no channel'},
        {'agent': A, 'channel': 'general', 'body': ''},
        {'agent': A, 'channel': 'general', 'body': 'x', 'to': ['Not A Name']},
        {'agent': A, 'channel': 'general', 'body': 'x', 'to': ['nobody_here_0000']},
    ]
    for body in bad:
        assert client.post('/api/messages', json=body).status_code == 422
    to_op = {'agent': A, 'channel': 'general', 'body': 'x', 'to': ['operator']}
    assert '/api/notify' in client.post('/api/messages', json=to_op).text
    bad_q = client.get('/api/inbox', params={'agent': 'Bad Name'})
    assert bad_q.status_code == 422 and 'register' in bad_q.text


def test_plain_text_bodies(client: TestClient, ab: tuple[str, str]) -> None:
    A, B = ab
    text = 'Don\'t merge: "core.py" fails on $EMPTY\n```\n`x` \\n é\n```'
    r = client.post('/api/messages', params={'agent': A, 'channel': 'general',
                    'to': f' {B} ,'}, content=f'{text}\n'.encode())
    assert r.status_code == 201, r.text
    assert r.json()['body'] == text and r.json()['recipients'] == [B]
    assert client.get('/api/inbox', params={'agent': B}).json()[0]['body'] == text

    n = client.post('/api/notify', params={'agent': A, 'priority': 'high'},
                    content=b'disk full\n')
    assert n.status_code == 201 and n.json()['body'] == 'disk full'
    assert n.json()['priority'] == 'high'

    def post(content: bytes, **params: str) -> int:
        q = {'agent': A, 'channel': 'general', **params}
        return client.post('/api/messages', params=q, content=content).status_code

    assert post(b'\n  \n') == 422                      # empty after trimming
    assert post(b'\xff\xfe') == 422                    # not UTF-8
    assert post(b'x', channel='Bad Channel') == 422
    assert post(b'x', to='nobody_here_0000') == 422
    assert post(b'x' * 8001) == 422
    assert post(b'x' * (64 * 1024 + 1)) == 413
    no_agent = client.post('/api/messages', params={'channel': 'general'},
                           content=b'x')
    assert no_agent.status_code == 422 and 'agent' in no_agent.text
    bad_prio = client.post('/api/notify', params={'agent': A, 'priority': 'max'},
                           content=b'x')
    assert bad_prio.status_code == 422


def test_plain_text_reads(client: TestClient, ab: tuple[str, str]) -> None:
    A, B = ab
    text = {'accept': 'text/plain'}
    body = 'Don\'t merge: "core.py"\n```\nmake test\n```'
    first = post(client, agent=A, channel='general', body=body, to=[B])
    post(client, agent=B, channel='general', body='second')
    head = f'=== #{first["id"]} from {A} to {B} in general at {first["created_at"]}'
    r = client.get('/api/inbox', params={'agent': B, 'peek': 1}, headers=text)
    assert r.headers['content-type'].startswith('text/plain')
    assert r.text == f'{head}\n{body}\n'
    listed = client.get('/api/messages', params={'channel': 'general'}, headers=text)
    assert listed.text.startswith(head) and f'from {B} in general' in listed.text
    one = client.get(f'/api/messages/{first["id"]}', headers=text)
    assert one.text == f'{head}\n{body}\n'
    waited = client.get('/api/wait', params={'agent': B, 'timeout': 0}, headers=text)
    assert waited.text == f'{head}\n{body}\n'
    empty = client.get('/api/wait', params={'agent': B, 'timeout': 0}, headers=text)
    assert empty.status_code == 200 and empty.text == ''
    assert client.get('/api/inbox', params={'agent': A}).json() == []  # JSON default
    missing = client.get('/api/messages/999', headers=text)
    assert missing.status_code == 404 and 'not found' in missing.text


def test_wait_returns_existing_or_times_out(
    client: TestClient, ab: tuple[str, str]
) -> None:
    A, B = ab
    assert client.get('/api/wait', params={'agent': B, 'timeout': 0.2}).json() == []
    post(client, agent=A, channel='general', body='ping', to=[B])
    msgs = client.get('/api/wait', params={'agent': B, 'timeout': 5}).json()
    assert [m['body'] for m in msgs] == ['ping']


def test_wait_wakes_on_new_message(settings: Settings) -> None:
    async def scenario() -> list[dict[str, Any]]:
        app = create_app(settings)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url='http://t') as c:
            reg = {'project': 'p', 'host': 'h'}
            A = (await c.post('/api/agents', json=reg)).json()['name']
            B = (await c.post('/api/agents', json=reg)).json()
            B = B['name']
            waiter = asyncio.create_task(
                c.get('/api/wait', params={'agent': B, 'timeout': 10})
            )
            await asyncio.sleep(0.2)
            states = {a['name']: a['state'] for a in (await c.get('/api/agents')).json()}
            assert states == {A: 'active', B: 'listening'}
            await c.post('/api/messages', json={
                'agent': A, 'channel': 'general', 'body': 'wake', 'to': [B]
            })
            msgs = (await asyncio.wait_for(waiter, 5)).json()
            states = {a['name']: a['state'] for a in (await c.get('/api/agents')).json()}
            assert states[B] == 'active'
            return msgs

    assert [m['body'] for m in asyncio.run(scenario())] == ['wake']


# =============================================================================
# Agent Sessions & Presence
# =============================================================================


def test_register_generates_session_names(client: TestClient) -> None:
    r = client.post('/api/agents', json={
        'project': 'Webshop Admin Console!', 'host': 'build-box.lan', 'tool': 'codex',
        'status': 'cart audit', 'meta': {'repo': 'git@x:webshop'},
    })
    assert r.status_code == 201
    a = r.json()
    assert AGENT_RE.match(a['name'])
    assert a['name'].startswith('webshop-admin-co_build-box-lan_')
    assert 'mode' not in a and a['state'] == 'active' and a['idle'] == 0
    assert a['meta'] == {'repo': 'git@x:webshop'} and a['tool'] == 'codex'
    names = {register(client, project='webshop', host='build-01') for _ in range(5)}
    assert len(names) == 5

    bad = [
        {'project': '!!!', 'host': 'h'},
        {'project': 'p'},
        {'project': 'p', 'host': 'h', 'meta': {'x': 'y' * 2000}},
    ]
    for body in bad:
        assert client.post('/api/agents', json=body).status_code == 422


def test_form_fields_replace_json(client: TestClient) -> None:
    r = client.post('/api/agents', data={
        'project': 'p', 'host': 'h', 'status': 'lead: planning',
        'meta': '{"role": "lead"}',
    })
    assert r.status_code == 201, r.text
    a = r.json()
    assert a['status'] == 'lead: planning' and a['meta'] == {'role': 'lead'}
    name = a['name']
    r = client.post(f'/api/agents/{name}', data={'status': 'lead: done'})
    assert r.json()['status'] == 'lead: done' and r.json()['meta'] == {'role': 'lead'}
    r = client.post(f'/api/agents/{name}', params={'status': 'via query'})
    assert r.json()['status'] == 'via query'
    bad = client.post(f'/api/agents/{name}', data={'meta': 'not json'})
    assert bad.status_code == 422 and 'meta' in bad.text
    typo = client.post(f'/api/agents/{name}', data={'stauts': 'x'})
    assert typo.status_code == 422 and 'allowed: status, meta' in typo.text
    # JSON sent with curl's default form content type is still read as JSON
    r = client.post(f'/api/agents/{name}', content=b'{"status": "NEW"}',
                    headers={'content-type': 'application/x-www-form-urlencoded'})
    assert r.json()['status'] == 'NEW'
    typo = client.post(f'/api/agents/{name}', json={'stauts': 'x'})
    assert typo.status_code == 422 and 'allowed: status, meta' in typo.text
    # habitual ?agent= on every call is ignored, not an error
    r = client.post(f'/api/agents/{name}', params={'agent': name}, data={'status': 's'})
    assert r.status_code == 200 and r.json()['status'] == 's'
    assert client.post(f'/api/agents/{name}', content=b'{oops').status_code == 422
    assert client.post(f'/api/agents/{name}', json=['x']).status_code == 422
    assert client.post(f'/api/agents/{name}', data={'mode': 'turn'}).status_code == 200
    misspelt = {'agent': name, 'channel': 'g', 'body': 'x', 'recipients': [name]}
    assert client.post('/api/messages', json=misspelt).status_code == 422
    assert client.post('/api/agents', data={'project': 'p'}).status_code == 422

    n = client.post('/api/notify', params={'agent': name}, content=b'help').json()
    r = client.post(f'/api/notify/{n["id"]}/resolve', data={'agent': name, 'note': 'ok'})
    assert r.status_code == 200 and r.json()['note'] == 'ok'


def test_get_one_agent_does_not_touch_it(
    client: TestClient, ab: tuple[str, str]
) -> None:
    A, _ = ab
    backdate(client, A, 700)
    one = client.get(f'/api/agents/{A}').json()
    assert one['name'] == A and one['state'] == 'stale' and one['idle'] >= 700
    assert client.get(f'/api/agents/{A}').json()['state'] == 'stale'
    assert client.get('/api/agents/ghost_nowhere_0000').status_code == 404


def test_register_can_post_the_join(client: TestClient) -> None:
    lead = register(client, project='p', host='mac')
    r = client.post('/api/agents', data={
        'project': 'p', 'host': 'dns', 'status': 'worker: A', 'channel': 'work',
        'lead': lead,
    })
    assert r.status_code == 201, r.text
    worker = r.json()
    joined = worker['joined']
    assert joined['body'] == f'{worker["name"]} joined: worker: A'
    assert joined['channel'] == 'work' and joined['recipients'] == [lead]
    inbox = client.get('/api/inbox', params={'agent': lead}).json()
    assert [m['id'] for m in inbox] == [joined['id']]

    # a lead joins without `to`; a replacement names the session it replaces
    r = client.post('/api/agents', data={'project': 'p', 'host': 'x', 'channel': 'work'})
    assert r.json()['joined']['recipients'] == []
    r = client.post('/api/agents', data={
        'project': 'p', 'host': 'dns', 'channel': 'work', 'lead': lead,
        'replaces': worker['name'],
    })
    assert r.json()['joined']['body'].endswith(f'joined (replacing {worker["name"]})')
    # a status that already names the old session is not repeated
    r = client.post('/api/agents', data={
        'project': 'p', 'host': 'dns', 'channel': 'work', 'lead': lead,
        'replaces': worker['name'], 'status': f'replacing {worker["name"]}',
    })
    assert r.json()['joined']['body'].count(worker['name']) == 1

    before = len(client.get('/api/agents', params={'all': 1}).json())
    for bad, why in [
        ({'lead': 'operator', 'channel': 'work'}, 'cannot be addressed'),
        ({'lead': 'ghost_nowhere_0000', 'channel': 'work'}, 'lead: no session named'),
        ({'replaces': 'ghost_nowhere_0000', 'channel': 'work'}, 'GET /api/agents'),
        ({'lead': '<lead name>', 'channel': 'work'}, 'no session named'),
        ({'lead': lead}, 'need a channel'),
        ({'channel': 'Bad Name!'}, 'invalid channel'),
    ]:
        r = client.post('/api/agents', data={'project': 'p', 'host': 'h', **bad})
        assert r.status_code == 422 and why in r.text, (bad, r.text)
    # rejected registrations create no session
    assert len(client.get('/api/agents', params={'all': 1}).json()) == before


def test_unregistered_names_are_rejected(client: TestClient) -> None:
    ghost = 'ghost_nowhere_0000'
    assert client.get('/api/inbox', params={'agent': ghost}).status_code == 404
    assert client.post('/api/agents/' + ghost, json={}).status_code == 404
    r = client.post('/api/messages', json={'agent': ghost, 'channel': 'g', 'body': 'x'})
    assert r.status_code == 404
    old_style = {'agent': 'claude@buildbox:infra', 'channel': 'g', 'body': 'x'}
    assert client.post('/api/messages', json=old_style).status_code == 422


def test_update_and_checkout(client: TestClient, ab: tuple[str, str]) -> None:
    A, _ = ab
    # an old client still sending mode is accepted; the field is ignored
    r = client.post(f'/api/agents/{A}', json={'status': 'migrating', 'mode': 'poll'})
    assert r.json()['status'] == 'migrating' and 'mode' not in r.json()
    r = client.post(f'/api/agents/{A}', json={'meta': {'k': 1}})
    assert r.json()['status'] == 'migrating' and r.json()['meta'] == {'k': 1}

    assert client.delete(f'/api/agents/{A}').json()['state'] == 'offline'
    assert agent(client, A)['state'] == 'offline'
    client.get('/api/inbox', params={'agent': A})  # seen again: back online
    assert agent(client, A)['state'] == 'active'


def test_checkout_during_wait_sticks(settings: Settings) -> None:
    async def scenario() -> list[str]:
        app = create_app(settings)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url='http://t') as c:
            reg = {'project': 'p', 'host': 'h'}
            name = (await c.post('/api/agents', json=reg)).json()['name']
            waiter = asyncio.create_task(
                c.get('/api/wait', params={'agent': name, 'timeout': 0.5})
            )
            await asyncio.sleep(0.1)
            states = [(await c.delete(f'/api/agents/{name}')).json()['state']]
            await asyncio.wait_for(waiter, 5)
            listed = (await c.get('/api/agents')).json()
            return [*states, listed[0]['state']]

    assert asyncio.run(scenario()) == ['offline', 'offline']


def test_wait_leaves_messages_unread_when_client_hangs_up(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(grimoire_app, 'DISCONNECT_CHECK', 0.05)
    app = create_app(settings)

    async def scenario() -> tuple[list[dict[str, Any]], int]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url='http://t') as c:
            reg = {'project': 'p', 'host': 'h'}
            A = (await c.post('/api/agents', json=reg)).json()['name']
            B = (await c.post('/api/agents', json=reg)).json()['name']
        events = iter([
            {'type': 'http.request', 'body': b'', 'more_body': False},
            {'type': 'http.disconnect'},
        ])
        sent: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            return next(events)

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        scope = {
            'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
            'method': 'GET', 'scheme': 'http', 'path': '/api/wait',
            'raw_path': b'/api/wait', 'root_path': '',
            'query_string': f'agent={B}&timeout=5'.encode(),
            'headers': [(b'host', b't')], 'client': ('127.0.0.1', 1),
            'server': ('t', 80),
        }
        async def deliver_late() -> None:
            # arrives while the wait is open, after the client has hung up
            await asyncio.sleep(0.01)
            app.state.db.add_message(A, channel='g', body='for B', recipients=[B])

        await asyncio.gather(app(scope, receive, send), deliver_late())
        return app.state.db.inbox(B, peek=True), sent[0]['status']

    unread, status = asyncio.run(scenario())
    assert status == 499 and [m['body'] for m in unread] == ['for B']


def test_shutdown_ends_open_waits_with_503(settings: Settings) -> None:
    app = create_app(settings)

    async def scenario() -> tuple[int, str, list[dict[str, Any]]]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url='http://t') as c:
            B = (await c.post('/api/agents', json={'project': 'p', 'host': 'h'}))
            B = B.json()['name']
            waiter = asyncio.create_task(
                c.get('/api/wait', params={'agent': B, 'timeout': 30})
            )
            await asyncio.sleep(0.1)
            await app.state.close_waits()
            r = await asyncio.wait_for(waiter, 5)
            return r.status_code, r.text, app.state.db.inbox(B, peek=True)

    status, text, unread = asyncio.run(scenario())
    assert status == 503 and 'call again' in text and unread == []


def test_wait_returns_a_burst_together(settings: Settings) -> None:
    app = create_app(settings)

    async def scenario() -> list[str]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url='http://t') as c:
            reg = {'project': 'p', 'host': 'h'}
            A = (await c.post('/api/agents', json=reg)).json()['name']
            B = (await c.post('/api/agents', json=reg)).json()['name']
            waiter = asyncio.create_task(
                c.get('/api/wait', params={'agent': B, 'timeout': 10})
            )
            await asyncio.sleep(0.1)
            for body in ('first', 'second'):
                await c.post('/api/messages', json={
                    'agent': A, 'channel': 'g', 'body': body, 'to': [B],
                })
                await asyncio.sleep(0.2)
            r = await asyncio.wait_for(waiter, 5)
            return [m['body'] for m in r.json()]

    assert asyncio.run(scenario()) == ['first', 'second']


def test_staleness_after_timeout(client: TestClient, ab: tuple[str, str]) -> None:
    A, B = ab
    backdate(client, A, 590)
    backdate(client, B, 601)
    a, b = agent(client, A), agent(client, B)
    assert a['state'] == 'active' and 590 <= a['idle'] < 600
    assert b['state'] == 'stale' and b['idle'] >= 601


def test_default_list_hides_old_sessions(
    client: TestClient, ab: tuple[str, str]
) -> None:
    A, B = ab
    backdate(client, A, 25 * 3600)
    backdate(client, B, 3600)
    listed = [a['name'] for a in client.get('/api/agents').json()]
    assert listed == [B]
    everyone = client.get('/api/agents', params={'all': 1}).json()
    assert {a['name'] for a in everyone} == {A, B}


def test_register_prunes_expired_sessions(
    client: TestClient, ab: tuple[str, str]
) -> None:
    A, B = ab
    backdate(client, A, 8 * 86400)
    register(client)
    assert client.get('/api/inbox', params={'agent': A}).status_code == 404
    assert client.get('/api/inbox', params={'agent': B}).status_code == 200


def test_new_session_never_sees_older_mail(
    client: TestClient, ab: tuple[str, str]
) -> None:
    A, B = ab
    post(client, agent=A, channel='general', body='before', to=[B])
    # simulate a later session that happens to receive the same name
    db = client.app.state.db  # type: ignore[attr-defined]
    db.conn.execute('DELETE FROM agents WHERE name = ?', (B,))
    db.register_agent(B, 'app', 'db01', '', '', {})
    assert client.get('/api/inbox', params={'agent': B}).json() == []
    post(client, agent=A, channel='general', body='after', to=[B])
    inbox = client.get('/api/inbox', params={'agent': B}).json()
    assert [m['body'] for m in inbox] == ['after']


def test_post_warns_about_departed_recipients(
    client: TestClient, ab: tuple[str, str]
) -> None:
    A, B = ab
    assert 'warnings' not in post(client, agent=A, channel='g', body='x', to=[B])
    backdate(client, B, 601)
    msg = post(client, agent=A, channel='g', body='x', to=[B])
    assert msg['warnings'] == [f'{B} is stale']


# =============================================================================
# Operator Notices
# =============================================================================


def test_notice_push_and_list(client: TestClient, ab: tuple[str, str]) -> None:
    A, B = ab
    n1 = notify(client, agent=A, body='approve backup prune?', priority='high')
    n2 = notify(client, agent=B, body='need disk on db01')
    pushed = client.published  # type: ignore[attr-defined]
    assert pushed[0] == {
        'topic': 'secret-topic',
        'title': f'{A} needs you',
        'message': 'approve backup prune?',
        'priority': 4,
        'click': f'http://grimoire.test:8080/ui#n{n1["id"]}',
    }
    assert len(pushed) == 2

    client.post(f'/api/notify/{n1["id"]}/resolve', json={'agent': B, 'note': 'ok'})
    listed = client.get('/api/notify').json()
    assert [n['id'] for n in listed] == [n2['id'], n1['id']]
    assert listed[1]['agent'] == A and listed[1]['resolved_by'] == B
    open_ = client.get('/api/notify', params={'open': 1}).json()
    assert [n['id'] for n in open_] == [n2['id']]


def test_resolve_is_idempotent_and_validated(
    client: TestClient, ab: tuple[str, str]
) -> None:
    A, B = ab
    n = notify(client, agent=A, body='help')
    url = f'/api/notify/{n["id"]}/resolve'
    first = client.post(url, json={'agent': B, 'note': 'done'}).json()
    again = client.post(url, json={'agent': A, 'note': 'late'}).json()
    assert first['resolved_by'] == B and again == first
    assert client.post('/api/notify/999/resolve', json={'agent': A}).status_code == 404
    assert client.post(url, json={'agent': 'operator'}).status_code == 422
    too_long = {'agent': A, 'body': 'x' * 1001}
    assert client.post('/api/notify', json=too_long).status_code == 422


def test_ntfy_publish_sends_auth_and_payload(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    cfg = NtfyConfig(server='https://ntfy.example', topic='t', token='tk')
    n = NtfyNotifier(cfg, 'http://g')
    n.client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), headers=n.client.headers
    )
    notice = {'id': 1, 'agent': 'a_b_c123', 'body': 'b', 'priority': 'urgent'}
    asyncio.run(n.publish(notice))
    assert requests[0].url == 'https://ntfy.example'
    assert requests[0].headers['authorization'] == 'Bearer tk'


# =============================================================================
# UI
# =============================================================================


def test_ui_renders_posts_and_escapes(client: TestClient, ab: tuple[str, str]) -> None:
    A, _ = ab
    post(client, agent=A, channel='general', body='<script>x</script>')
    notify(client, agent=A, body='needs <b>you</b>')
    html = client.get('/ui').text
    assert '&lt;script&gt;x' in html and 'needs &lt;b&gt;you' in html
    post(client, agent=A, channel='general', body='second post')
    html = client.get('/ui').text
    assert html.index('second post') < html.index('&lt;script&gt;x')  # newest first
    assert 'Needs you' in html
    agents = client.get('/ui/agents').text
    assert f'id="a-{A}"' in agents and 'st-active' in agents

    r = client.post('/ui/post', data={'channel': 'general', 'body': 'freeze', 'to': A},
                    follow_redirects=False)
    assert r.status_code == 303
    inbox = client.get('/api/inbox', params={'agent': A}).json()
    assert inbox[0]['agent'] == 'operator' and inbox[0]['body'] == 'freeze'
    bad = client.post('/ui/post', data={'channel': '', 'body': 'x'})
    assert bad.status_code == 422
    big = client.post('/ui/post', data={'channel': 'general', 'body': 'x' * 8001})
    assert big.status_code == 422 and 'exceeds 8000' in big.text
    ghost = {'channel': 'general', 'body': 'keep me', 'to': 'ghost_nowhere_0000'}
    r = client.post('/ui/post', data=ghost)
    assert r.status_code == 422
    assert 'class="flash" role="alert">unknown recipients' in r.text
    assert '>keep me</textarea>' in r.text

    backdate(client, A, 3600)
    r = client.post('/ui/post', data={'channel': 'general', 'body': 'y', 'to': A})
    assert r.status_code == 200 and f'{A} is stale' in r.text  # after redirect


def test_ui_pages(client: TestClient, ab: tuple[str, str]) -> None:
    A, B = ab
    post(client, agent=A, channel='general', body='first ' + 'x' * 80)
    post(client, agent=B, channel='general', body='```\ncode <b>\n```\nsee `x`')
    post(client, agent=A, channel='ops', body='| a | b |\n| 1 | 2 |')
    post(client, agent=B, channel='ops', body=f'ping {A}, not `{A}`')
    notify(client, agent=A, body='low one', priority='low')
    notify(client, agent=B, body='fire', priority='urgent')

    html = client.get('/ui').text
    assert html.index('fire') < html.index('low one')  # most pressing first
    assert '<pre>code &lt;b&gt;</pre>' in html and '<code>x</code>' in html
    assert '<pre>| a | b |\n| 1 | 2 |</pre>' in html
    assert '<title>(2) ' in html and 'class="count"' in html
    short = A.split('_', 1)[1]
    assert f'ping <a href="/ui/agents#a-{A}" title="{A}">{short}</a>' in html
    assert f'<code>{A}</code>' in html  # names in code stay as written
    assert html.index('Urgent notice') < html.index('Low notice')  # rail by priority

    channels = client.get('/ui/channels').text
    assert '2 agents, 2 messages' in channels and f'{short}</b>: first' in channels
    assert 'x' * 61 not in channels  # preview cut at 60 characters
    by_name = client.get('/ui/channels', params={'sort': 'name', 'previews': 0}).text
    assert by_name.index('#general') < by_name.index('#ops')
    assert 'class="preview"' not in by_name

    agents = client.get('/ui/agents').text
    assert f'data-to="{A}" data-channel="ops"' in agents  # latest channel preset
    assert 'data-label="Messages">2</td>' in agents
    assert f'<span class="proj">{A.split("_")[0]}_<wbr></span>' in agents
    assert '>Low</span>' in html and '>Urgent</span>' in html  # priority in words
    page = client.get('/ui/channels/general').text
    assert 'aria-label="Agents in this channel"' in page and f'#a-{B}' in page
    assert 'aria-label="Storage used"' in client.get('/ui/files').text
    assert '#general' in client.get('/ui/channels/general').text

    old = client.get('/ui', params={'channel': 'ops'}, follow_redirects=False)
    assert old.headers['location'] == '/ui/channels/ops'
    old = client.get('/ui', params={'agents': 'all'}, follow_redirects=False)
    assert old.headers['location'] == '/ui/agents?all=1'


def test_favicon(client: TestClient) -> None:
    for path in ('/favicon.svg', '/favicon.ico'):
        r = client.get(path)
        assert r.status_code == 200 and r.text.startswith('<svg')
        assert r.headers['content-type'] == 'image/svg+xml'
    assert '<link rel="icon" href="/favicon.svg"' in client.get('/ui').text


# =============================================================================
# Files
# =============================================================================


def upload(c: TestClient, agent: str, content: bytes, name: str = 'a.log') -> Any:
    return c.post('/api/files', params={'agent': agent, 'name': name}, content=content)


def blobs(settings: Settings) -> list[Path]:
    return sorted(settings.files_dir.glob('??/*'))


def test_file_upload_download_and_dedupe(
    client: TestClient, settings: Settings, ab: tuple[str, str]
) -> None:
    A, B = ab
    data = b'line one\nline two\n'
    sha = hashlib.sha256(data).hexdigest()
    r = upload(client, A, data, name='logs/../dir\\build.log')
    assert r.status_code == 201, r.text
    f = r.json()
    assert f['name'] == 'build.log' and f['size'] == len(data)
    assert f['status'] == 'stored' and f['sha256'] == sha and f['agent'] == A
    assert len(f['id']) == 8 and f['url'].endswith(f'/api/files/{f["id"]}')

    got = client.get(f'/api/files/{f["id"]}')
    assert got.status_code == 200 and got.content == data
    assert got.headers['x-sha256'] == sha
    assert 'filename="build.log"' in got.headers['content-disposition']

    # same content from another agent: new record, one blob on disk
    g = upload(client, B, data, name='copy.log').json()
    assert g['id'] != f['id'] and g['sha256'] == sha
    assert blobs(settings) == [settings.files_dir / sha[:2] / sha]
    assert [x['id'] for x in client.get('/api/files').json()] == [g['id'], f['id']]
    assert not list((settings.files_dir / 'tmp').iterdir())

    client.post('/api/messages', json={
        'agent': A, 'channel': 'general', 'body': f'see file:{f["id"]}'
    })
    assert f'<a href="/api/files/{f["id"]}">file:{f["id"]}</a>' in client.get('/ui').text
    assert client.get('/api/files/nope1234').status_code == 404


def test_file_info_and_delete(
    client: TestClient, settings: Settings, ab: tuple[str, str]
) -> None:
    A, B = ab
    f = upload(client, A, b'shared').json()
    g = upload(client, B, b'shared').json()
    h = upload(client, A, b'solo').json()

    info = client.get(f'/api/files/{f["id"]}/info')
    assert info.status_code == 200 and info.json() == f
    assert client.get('/api/files/nope1234/info').status_code == 404

    # uploader only; unknown agents must register first
    assert client.delete(f'/api/files/{f["id"]}', params={'agent': B}).status_code == 403
    gone = client.delete(f'/api/files/{f["id"]}', params={'agent': 'ghost_x_0000'})
    assert gone.status_code == 404

    r = client.delete(f'/api/files/{f["id"]}', params={'agent': A})
    assert r.status_code == 200 and r.json()['id'] == f['id']
    assert r.json()['status'] == 'withdrawn'
    gone = client.get(f'/api/files/{f["id"]}')
    assert gone.status_code == 410 and f'withdrawn by its uploader {A}' in gone.text
    assert client.get(f'/api/files/{f["id"]}/info').status_code == 410
    assert client.delete(f'/api/files/{f["id"]}', params={'agent': A}).status_code == 410
    assert f['id'] not in [x['id'] for x in client.get('/api/files').json()]
    # g still references the shared blob; h's blob goes as soon as h is deleted
    assert client.get(f'/api/files/{g["id"]}').content == b'shared'
    assert client.get(f'/api/files/file:{g["id"]}/info').json()['id'] == g['id']
    assert len(blobs(settings)) == 2
    # the file: prefix pasted from a message withdraws the same file
    client.delete(f'/api/files/file:{h["id"]}', params={'agent': A})
    assert client.get(f'/api/files/{h["id"]}').status_code == 410
    assert blobs(settings) == [settings.files_dir / g['sha256'][:2] / g['sha256']]


def test_file_upload_limits(settings: Settings, tmp_path: Path) -> None:
    files = FilesConfig(max_size=200, max_total=250)
    with TestClient(create_app(dataclasses.replace(settings, files=files))) as c:
        A = register(c)
        assert upload(c, 'ghost_nowhere_0000', b'x').status_code == 404
        assert upload(c, A, b'').status_code == 422
        assert upload(c, A, b'x', name='dir/').status_code == 422
        assert upload(c, A, b'x' * 201).status_code == 413
        chunked = c.post('/api/files', params={'agent': A, 'name': 'a'},
                         content=iter([b'x']))
        assert chunked.status_code == 411
        assert upload(c, A, b'a' * 100).status_code == 201
        # a re-upload needs staging room, but dedupes to no extra usage after
        assert upload(c, A, b'a' * 100).status_code == 201
        full = upload(c, A, b'b' * 151)
        assert full.status_code == 507 and 'full' in full.text
        assert upload(c, A, b'b' * 150).status_code == 201


def test_interrupted_upload_is_cleaned_up(settings: Settings) -> None:
    app = create_app(settings)

    async def scenario() -> list[dict[str, Any]]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url='http://t') as c:
            A = (await c.post('/api/agents', json={'project': 'p', 'host': 'h'}))
            A = A.json()['name']
        events = iter([
            {'type': 'http.request', 'body': b'part', 'more_body': True},
            {'type': 'http.disconnect'},
        ])
        sent: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            return next(events)

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        scope = {
            'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
            'method': 'POST', 'scheme': 'http', 'path': '/api/files',
            'raw_path': b'/api/files', 'root_path': '',
            'query_string': f'agent={A}&name=cut.bin'.encode(),
            'headers': [(b'host', b't'), (b'content-length', b'100')],
            'client': ('127.0.0.1', 1), 'server': ('t', 80),
        }
        await app(scope, receive, send)
        return sent

    sent = asyncio.run(scenario())
    assert sent[0]['status'] == 400
    assert not list((settings.files_dir / 'tmp').iterdir())
    assert app.state.db.files_usage() == 0  # the uploading row is gone too


def test_file_retention_tiers() -> None:
    files = FilesConfig()
    assert files.ttl(50 * MB) == 7 * 86400
    assert files.ttl(50 * MB + 1) == 24 * 3600
    assert files.ttl(500 * MB + 1) == 2 * 3600


def test_file_garbage_collection(
    client: TestClient, settings: Settings, ab: tuple[str, str]
) -> None:
    A, B = ab
    f = upload(client, A, b'shared').json()
    g = upload(client, B, b'shared').json()
    upload(client, A, b'other')
    stray = settings.files_dir / 'ab' / ('ab' + '0' * 62)
    stray.parent.mkdir(exist_ok=True)
    stray.write_bytes(b'not in the database')
    db = client.app.state.db

    def expire(file_id: str) -> None:
        db.conn.execute(
            "UPDATE files SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (file_id,),
        )
        db.conn.commit()

    expire(f['id'])
    assert client.get(f'/api/files/{f["id"]}').status_code == 404
    client.app.state.collect_files()
    assert not stray.exists() and len(blobs(settings)) == 2  # g keeps the shared blob
    expire(g['id'])
    client.app.state.collect_files()
    assert len(blobs(settings)) == 1 and len(client.get('/api/files').json()) == 1


def test_startup_clears_abandoned_uploads(settings: Settings) -> None:
    with TestClient(create_app(settings)) as c:
        A = register(c)
        c.app.state.db.add_upload('crashed1', A, 'big.iso', 10)
    (settings.files_dir / 'tmp' / 'crashed1').write_bytes(b'partial')
    with TestClient(create_app(settings)) as c:
        assert not c.app.state.db.file_exists('crashed1')
        assert not list((settings.files_dir / 'tmp').iterdir())
