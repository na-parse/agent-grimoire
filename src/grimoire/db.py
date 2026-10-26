'''SQLite persistence for board messages, operator notices, presence, inbox
cursors and file records (file contents live in ``files.FileStore``).

All access happens on the asyncio event loop thread (endpoints are async and
queries are sub-millisecond at LAN scale), so a single connection is sufficient.
'''

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# =============================================================================
# Schema
# =============================================================================

SCHEMA = '''
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    agent       TEXT NOT NULL,
    channel     TEXT NOT NULL,
    recipients  TEXT NOT NULL DEFAULT '[]',
    body        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel, id);

CREATE TABLE IF NOT EXISTS notices (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    agent       TEXT NOT NULL,
    priority    TEXT NOT NULL DEFAULT 'default',
    body        TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT,
    note        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS agents (
    name        TEXT PRIMARY KEY,
    project     TEXT NOT NULL,
    host        TEXT NOT NULL,
    tool        TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT '',
    meta        TEXT NOT NULL DEFAULT '{}',
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    offline_at  TEXT
);

CREATE TABLE IF NOT EXISTS cursors (
    agent       TEXT PRIMARY KEY,
    last_id     INTEGER NOT NULL DEFAULT 0
);

-- one row per upload; rows with the same sha256 share one blob on disk
CREATE TABLE IF NOT EXISTS files (
    id          TEXT PRIMARY KEY,
    agent       TEXT NOT NULL,
    name        TEXT NOT NULL,
    size        INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'uploading',
    sha256      TEXT,
    created_at  TEXT NOT NULL,
    expires_at  TEXT
);
'''


def _now(offset: float = 0) -> str:
    '''UTC time ``offset`` seconds from now as an ISO-8601 string (seconds).'''
    when = datetime.now(UTC) + timedelta(seconds=offset)
    return when.isoformat(timespec='seconds')


def _row_to_agent(row: sqlite3.Row) -> dict[str, Any]:
    '''Convert an agents row into an API-friendly dict.'''
    agent = dict(row)
    agent['meta'] = json.loads(agent['meta'])
    return agent


def _row_to_message(row: sqlite3.Row) -> dict[str, Any]:
    '''Convert a messages row into an API-friendly dict.'''
    msg = dict(row)
    msg['recipients'] = json.loads(msg['recipients'])
    return msg


# =============================================================================
# Database
# =============================================================================


class GrimoireDB:
    '''Thin data-access layer over a single SQLite connection.'''

    def __init__(self, path: Path | str) -> None:
        if str(path) != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        '''Close the underlying connection.'''
        self.conn.close()

    # -------------------------------------------------------------------------
    # Agents
    # -------------------------------------------------------------------------

    def get_agent(self, name: str) -> dict[str, Any] | None:
        '''Fetch a registered agent session by name.'''
        row = self.conn.execute(
            'SELECT * FROM agents WHERE name = ?', (name,)
        ).fetchone()
        return _row_to_agent(row) if row else None

    def register_agent(
        self,
        name: str,
        project: str,
        host: str,
        tool: str,
        status: str,
        meta: dict[str, Any],
    ) -> dict[str, Any]:
        '''Create an agent session.

        The inbox cursor starts at the newest message, so a session never receives
        mail sent before it existed (even if a pruned name is ever generated again).
        '''
        now = _now()
        self.conn.execute(
            '''INSERT INTO agents
                 (name, project, host, tool, status, meta, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
            (name, project, host, tool, status, json.dumps(meta), now, now),
        )
        self.conn.execute(
            '''INSERT OR REPLACE INTO cursors (agent, last_id)
               VALUES (?, (SELECT COALESCE(MAX(id), 0) FROM messages))''',
            (name,),
        )
        self.conn.commit()
        agent = self.get_agent(name)
        assert agent is not None
        return agent

    def update_agent(
        self,
        name: str,
        status: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        '''Update the given fields of an agent session (``None`` = unchanged).'''
        self.conn.execute(
            '''UPDATE agents SET status = COALESCE(?, status),
                 meta = COALESCE(?, meta)
               WHERE name = ?''',
            (status, None if meta is None else json.dumps(meta), name),
        )
        self.conn.commit()
        agent = self.get_agent(name)
        assert agent is not None
        return agent

    def touch_agent(self, name: str, revive: bool = True) -> None:
        '''Record that an agent was seen; ``revive`` also clears a check-out.'''
        clear = ', offline_at = NULL' if revive else ''
        self.conn.execute(
            f'UPDATE agents SET last_seen = ?{clear} WHERE name = ?', (_now(), name)
        )
        self.conn.commit()

    def checkout_agent(self, name: str) -> dict[str, Any]:
        '''Mark an agent session offline; the row is kept until pruned.'''
        now = _now()
        self.conn.execute(
            'UPDATE agents SET last_seen = ?, offline_at = ? WHERE name = ?',
            (now, now, name),
        )
        self.conn.commit()
        agent = self.get_agent(name)
        assert agent is not None
        return agent

    def list_agents(self) -> list[dict[str, Any]]:
        '''All agent sessions, most recently seen first.'''
        rows = self.conn.execute('SELECT * FROM agents ORDER BY last_seen DESC')
        return [_row_to_agent(r) for r in rows]

    def prune_agents(self, before: str) -> None:
        '''Delete sessions (and their inbox cursors) last seen before ``before``.'''
        self.conn.execute(
            '''DELETE FROM cursors WHERE agent IN
                 (SELECT name FROM agents WHERE last_seen < ?)''',
            (before,),
        )
        self.conn.execute('DELETE FROM agents WHERE last_seen < ?', (before,))
        self.conn.commit()

    # -------------------------------------------------------------------------
    # Messages
    # -------------------------------------------------------------------------

    def get_message(self, message_id: int) -> dict[str, Any] | None:
        '''Fetch a single message by id.'''
        row = self.conn.execute(
            'SELECT * FROM messages WHERE id = ?', (message_id,)
        ).fetchone()
        return _row_to_message(row) if row else None

    def add_message(
        self,
        agent: str,
        channel: str,
        body: str,
        recipients: list[str] | None = None,
    ) -> dict[str, Any]:
        '''Insert a board message and return it.'''
        cur = self.conn.execute(
            '''INSERT INTO messages (created_at, agent, channel, recipients, body)
               VALUES (?, ?, ?, ?, ?)''',
            (_now(), agent, channel, json.dumps(list(dict.fromkeys(recipients or []))),
             body),
        )
        self.conn.commit()
        msg = self.get_message(cur.lastrowid)
        assert msg is not None
        return msg

    def list_messages(
        self,
        channel: str | None = None,
        since: int | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        '''List messages in ascending id order.

        With ``since``: the first ``limit`` messages after that id.
        Without: the most recent ``limit`` messages.
        '''
        where: list[str] = []
        args: list[Any] = []
        if channel is not None:
            where.append('channel = ?')
            args.append(channel)
        if since is not None:
            where.append('id > ?')
            args.append(since)
        clause = f'WHERE {" AND ".join(where)}' if where else ''
        order = 'ASC' if since is not None else 'DESC'
        rows = self.conn.execute(
            f'SELECT * FROM messages {clause} ORDER BY id {order} LIMIT ?',
            (*args, limit),
        ).fetchall()
        msgs = [_row_to_message(r) for r in rows]
        return msgs if since is not None else msgs[::-1]

    def list_channels(self) -> list[dict[str, Any]]:
        '''Channels that contain messages, with counts and last activity.

        ``agents`` counts the distinct sessions that posted in the channel.
        '''
        rows = self.conn.execute(
            '''SELECT channel AS name, COUNT(*) AS messages,
                      COUNT(DISTINCT agent) AS agents,
                      MAX(id) AS last_id, MAX(created_at) AS last_at
               FROM messages GROUP BY channel ORDER BY last_id DESC'''
        )
        return [dict(r) for r in rows]

    def channel_previews(self, per: int = 2) -> dict[str, list[dict[str, Any]]]:
        '''The ``per`` newest messages of every channel, newest first.'''
        rows = self.conn.execute(
            '''SELECT * FROM (SELECT *, ROW_NUMBER() OVER
                 (PARTITION BY channel ORDER BY id DESC) AS rank FROM messages)
               WHERE rank <= ? ORDER BY id DESC''',
            (per,),
        )
        previews: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            msg = _row_to_message(r)
            del msg['rank']
            previews.setdefault(msg['channel'], []).append(msg)
        return previews

    def agent_activity(self) -> dict[str, dict[str, Any]]:
        '''Per posting session: message count and channels, most recent first.'''
        rows = self.conn.execute(
            '''SELECT agent, channel, COUNT(*) AS messages FROM messages
               GROUP BY agent, channel ORDER BY MAX(id) DESC'''
        )
        activity: dict[str, dict[str, Any]] = {}
        for r in rows:
            entry = activity.setdefault(r['agent'], {'messages': 0, 'channels': []})
            entry['messages'] += r['messages']
            entry['channels'].append(r['channel'])
        return activity

    # -------------------------------------------------------------------------
    # Inbox
    # -------------------------------------------------------------------------

    def inbox(
        self, agent: str, peek: bool = False, limit: int = 100
    ) -> list[dict[str, Any]]:
        '''Unread messages addressed to ``agent``; marks them read unless ``peek``.'''
        row = self.conn.execute(
            'SELECT last_id FROM cursors WHERE agent = ?', (agent,)
        ).fetchone()
        cursor = row['last_id'] if row else 0
        rows = self.conn.execute(
            '''SELECT * FROM messages m
               WHERE m.id > ? AND m.agent != ?
                 AND EXISTS (SELECT 1 FROM json_each(m.recipients) WHERE value = ?)
               ORDER BY m.id ASC LIMIT ?''',
            (cursor, agent, agent, limit),
        ).fetchall()
        msgs = [_row_to_message(r) for r in rows]
        if msgs and not peek:
            self.conn.execute(
                '''INSERT INTO cursors (agent, last_id) VALUES (?, ?)
                   ON CONFLICT(agent) DO UPDATE SET last_id = excluded.last_id''',
                (agent, msgs[-1]['id']),
            )
            self.conn.commit()
        return msgs

    # -------------------------------------------------------------------------
    # Notices
    # -------------------------------------------------------------------------

    def get_notice(self, notice_id: int) -> dict[str, Any] | None:
        '''Fetch a single operator notice by id.'''
        row = self.conn.execute(
            'SELECT * FROM notices WHERE id = ?', (notice_id,)
        ).fetchone()
        return dict(row) if row else None

    def add_notice(self, agent: str, body: str, priority: str) -> dict[str, Any]:
        '''Insert an operator notice and return it.'''
        cur = self.conn.execute(
            '''INSERT INTO notices (created_at, agent, priority, body)
               VALUES (?, ?, ?, ?)''',
            (_now(), agent, priority, body),
        )
        self.conn.commit()
        notice = self.get_notice(cur.lastrowid)
        assert notice is not None
        return notice

    def list_notices(
        self, open_only: bool = False, limit: int = 50
    ) -> list[dict[str, Any]]:
        '''Notices with open ones first (oldest first), then resolved (newest first).'''
        clause = 'WHERE resolved_at IS NULL' if open_only else ''
        rows = self.conn.execute(
            f'''SELECT * FROM notices {clause}
                ORDER BY resolved_at IS NOT NULL,
                         CASE WHEN resolved_at IS NULL THEN id ELSE -id END
                LIMIT ?''',
            (limit,),
        )
        return [dict(r) for r in rows]

    def resolve_notice(self, notice_id: int, agent: str, note: str) -> dict[str, Any]:
        '''Mark a notice resolved (idempotent: keeps the first resolution).

        Raises:
            LookupError: the notice does not exist.
        '''
        self.conn.execute(
            '''UPDATE notices SET resolved_at = ?, resolved_by = ?, note = ?
               WHERE id = ? AND resolved_at IS NULL''',
            (_now(), agent, note, notice_id),
        )
        self.conn.commit()
        notice = self.get_notice(notice_id)
        if notice is None:
            raise LookupError(f'notice {notice_id} not found')
        return notice

    # -------------------------------------------------------------------------
    # Files
    # -------------------------------------------------------------------------

    def add_upload(self, file_id: str, agent: str, name: str, size: int) -> None:
        '''Record an upload in progress (``size`` is the declared size).'''
        self.conn.execute(
            '''INSERT INTO files (id, agent, name, size, created_at)
               VALUES (?, ?, ?, ?, ?)''',
            (file_id, agent, name, size, _now()),
        )
        self.conn.commit()

    def store_file(
        self, file_id: str, sha256: str, size: int, ttl: float
    ) -> dict[str, Any]:
        '''Mark an upload stored with its hash, actual size and expiry.'''
        self.conn.execute(
            '''UPDATE files SET status = 'stored', sha256 = ?, size = ?,
                 expires_at = ? WHERE id = ?''',
            (sha256, size, _now(ttl), file_id),
        )
        self.conn.commit()
        record = self.get_file(file_id)
        assert record is not None
        return record

    def drop_file(self, file_id: str) -> None:
        '''Delete a file record (used for failed uploads).'''
        self.conn.execute('DELETE FROM files WHERE id = ?', (file_id,))
        self.conn.commit()

    def withdraw_file(self, file_id: str) -> None:
        '''Mark a stored file withdrawn: kept as a tombstone until it expires.'''
        self.conn.execute(
            "UPDATE files SET status = 'withdrawn' WHERE id = ?", (file_id,)
        )
        self.conn.commit()

    def get_withdrawn(self, file_id: str) -> dict[str, Any] | None:
        '''Fetch the tombstone of a withdrawn, unexpired file.'''
        row = self.conn.execute(
            '''SELECT * FROM files
               WHERE id = ? AND status = 'withdrawn' AND expires_at > ?''',
            (file_id, _now()),
        ).fetchone()
        return dict(row) if row else None

    def file_exists(self, file_id: str) -> bool:
        '''True if any record (uploading, stored or expired) uses ``file_id``.'''
        row = self.conn.execute('SELECT 1 FROM files WHERE id = ?', (file_id,))
        return row.fetchone() is not None

    def get_file(self, file_id: str) -> dict[str, Any] | None:
        '''Fetch a stored, unexpired file record.'''
        row = self.conn.execute(
            '''SELECT * FROM files
               WHERE id = ? AND status = 'stored' AND expires_at > ?''',
            (file_id, _now()),
        ).fetchone()
        return dict(row) if row else None

    def list_files(self, limit: int = 50) -> list[dict[str, Any]]:
        '''Stored, unexpired file records, newest first.'''
        rows = self.conn.execute(
            '''SELECT * FROM files WHERE status = 'stored' AND expires_at > ?
               ORDER BY created_at DESC, rowid DESC LIMIT ?''',
            (_now(), limit),
        )
        return [dict(r) for r in rows]

    def files_usage(self) -> int:
        '''Bytes on disk (each blob once) plus declared sizes of open uploads.'''
        row = self.conn.execute(
            '''SELECT
                 (SELECT COALESCE(SUM(size), 0) FROM (SELECT MAX(size) AS size
                    FROM files WHERE status = 'stored' GROUP BY sha256))
               + (SELECT COALESCE(SUM(size), 0) FROM files
                    WHERE status = 'uploading')'''
        ).fetchone()
        return int(row[0])

    def stored_hashes(self) -> set[str]:
        '''Hashes still referenced by a stored record (expired ones included).'''
        rows = self.conn.execute(
            "SELECT DISTINCT sha256 FROM files WHERE status = 'stored'"
        )
        return {r[0] for r in rows}

    def expire_files(self) -> None:
        '''Delete stored records and tombstones past their expiry.'''
        self.conn.execute(
            '''DELETE FROM files
               WHERE status IN ('stored', 'withdrawn') AND expires_at <= ?''',
            (_now(),),
        )
        self.conn.commit()

    def clear_uploads(self) -> None:
        '''Delete records of uploads in progress (only safe at startup).'''
        self.conn.execute("DELETE FROM files WHERE status = 'uploading'")
        self.conn.commit()
