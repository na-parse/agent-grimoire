'''ntfy push for operator notices (outbound only).

Every new notice is pushed to a single topic with its full body. There is
deliberately no inbound path: topics on a public server can be written by anyone
who knows the name, so the operator answers agents in their own sessions or via
``/ui``.
'''

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from typing import Any

import httpx

from .config import NtfyConfig, priority_rank

log = logging.getLogger(__name__)


# =============================================================================
# Notifier
# =============================================================================


class NtfyNotifier:
    '''Publishes operator notices to an ntfy topic in the background.'''

    def __init__(
        self,
        cfg: NtfyConfig,
        public_url: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.cfg = cfg
        self.public_url = public_url
        headers = {'Authorization': f'Bearer {cfg.token}'} if cfg.token else {}
        self.client = client or httpx.AsyncClient(headers=headers, timeout=15)
        self._tasks: set[asyncio.Task[None]] = set()

    async def aclose(self) -> None:
        '''Wait briefly for in-flight pushes, then close the HTTP client.'''
        if self._tasks:
            await asyncio.wait(self._tasks, timeout=5)
        await self.client.aclose()

    def build_payload(self, notice: dict[str, Any]) -> dict[str, Any]:
        '''Build the ntfy JSON publish payload for a notice.'''
        return {
            'topic': self.cfg.topic,
            'title': f'{notice["agent"]} needs you',
            'message': notice['body'],
            'priority': priority_rank(notice['priority']),
            'click': f'{self.public_url}/ui#n{notice["id"]}',
        }

    async def publish(self, notice: dict[str, Any]) -> None:
        '''Publish one notice (errors are logged, never raised).'''
        try:
            payload = self.build_payload(notice)
            resp = await self.client.post(self.cfg.server, json=payload)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning('ntfy publish failed for notice %s: %s', notice['id'], exc)

    def publish_later(self, notice: dict[str, Any]) -> None:
        '''Fire-and-forget publish so API calls never wait on ntfy.'''
        if self.cfg.enabled:
            self._spawn(self.publish(notice))

    def _spawn(self, coro: Awaitable[None]) -> None:
        '''Run a coroutine in the background, holding a reference until done.'''
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
