'''Entry point: ``grimoire`` / ``python -m grimoire``.'''

from __future__ import annotations

import argparse
import asyncio
import logging
import secrets
from types import FrameType

import uvicorn
from fastapi import FastAPI

from .app import create_app


class _Server(uvicorn.Server):
    '''Uvicorn server that ends open long-polls with a 503 as soon as it stops.'''

    def __init__(self, config: uvicorn.Config, app: FastAPI) -> None:
        super().__init__(config)
        self.app = app

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        loop: asyncio.AbstractEventLoop | None = getattr(self.app.state, 'loop', None)
        if loop is not None and not self.should_exit:
            close = self.app.state.close_waits
            # keep a reference: the loop holds tasks only weakly
            loop.call_soon_threadsafe(
                lambda: setattr(self, 'closing', loop.create_task(close()))
            )
        super().handle_exit(sig, frame)


def main() -> None:
    '''Run the Grimoire server, or print a fresh ntfy topic name.'''
    parser = argparse.ArgumentParser(prog='grimoire')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument(
        '--gen-topic', action='store_true',
        help='print a random ntfy topic as a .env line and exit',
    )
    args = parser.parse_args()

    if args.gen_topic:
        print(f'GRIMOIRE_NTFY_TOPIC=agent-grimoire-{secrets.token_urlsafe(16)}')
        return

    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')
    # open long-polls (up to 300 s) must not hold up a restart: they get a 503
    # at once (messages stay unread), and anything left is cut after 5 s
    app = create_app()
    config = uvicorn.Config(
        app, host=args.host, port=args.port, timeout_graceful_shutdown=5
    )
    _Server(config, app).run()


if __name__ == '__main__':
    main()
