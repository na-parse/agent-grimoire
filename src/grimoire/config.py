'''Configuration loading for Grimoire.

All settings come from environment variables (normally a compose ``.env``). The only
file-based setting is the optional operator prompt, which replaces the built-in
guidance on when agents may notify the operator.
'''

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

# =============================================================================
# Constants
# =============================================================================

PRIORITIES: tuple[str, ...] = ('min', 'low', 'default', 'high', 'urgent')
OPERATOR: str = 'operator'
BODY_MAX: int = 8000  # message body limit, API and operator form
MB: int = 1024**2
GB: int = 1024 * MB
# Files over these sizes (bytes) get the short retention tiers
LARGE_FILE: int = 50 * MB
HUGE_FILE: int = 500 * MB
CHANNEL_RE: re.Pattern[str] = re.compile(r'^(?!\d+$)[a-z0-9][a-z0-9_.-]{0,63}$')
# Session names are server-generated: <project[:16]>_<host[:16]>_<rand4>
AGENT_RE: re.Pattern[str] = re.compile(r'^[a-z0-9-]{1,16}_[a-z0-9-]{1,16}_[a-z0-9]{4}$')


def priority_rank(priority: str) -> int:
    '''Return the ntfy numeric priority (1-5) for a priority name.'''
    return PRIORITIES.index(priority) + 1


# =============================================================================
# Config Models
# =============================================================================


@dataclass(frozen=True)
class NtfyConfig:
    '''ntfy push settings for operator notices (outbound only).'''

    server: str = 'https://ntfy.sh'
    topic: str = ''
    token: str = ''

    @property
    def enabled(self) -> bool:
        '''True when a topic is configured.'''
        return bool(self.topic)


@dataclass(frozen=True)
class PresenceConfig:
    '''Presence timeouts: when an agent counts as stale, and when it is pruned.'''

    timeout: float = 600.0
    retention_days: float = 7.0


@dataclass(frozen=True)
class FilesConfig:
    '''File store limits and size-tiered retention (``max_total`` 0 = no quota).'''

    max_size: int = 2048 * MB
    max_total: int = 0
    retention_days: float = 7.0
    large_hours: float = 24.0
    huge_hours: float = 2.0

    def ttl(self, size: int) -> float:
        '''Seconds a stored file of ``size`` bytes is kept.'''
        if size > HUGE_FILE:
            return self.huge_hours * 3600
        if size > LARGE_FILE:
            return self.large_hours * 3600
        return self.retention_days * 86400


@dataclass(frozen=True)
class Settings:
    '''Top-level Grimoire settings.'''

    public_url: str = 'http://localhost:8080'
    db_path: Path = Path('data/grimoire.db')
    title: str = 'Grimoire'
    operator_prompt: str | None = None
    ntfy: NtfyConfig = NtfyConfig()
    presence: PresenceConfig = PresenceConfig()
    files: FilesConfig = FilesConfig()

    @property
    def files_dir(self) -> Path:
        '''File store root, next to the database.'''
        return self.db_path.parent / 'files'


# =============================================================================
# Loading
# =============================================================================


def _read_prompt(path: str) -> str | None:
    '''Read the operator prompt file if it exists and is non-empty.'''
    p = Path(path)
    if not path or not p.is_file():
        return None
    return p.read_text().strip() or None


def _clamp(raw: str | None, default: float, low: float, high: float) -> float:
    '''Parse a numeric setting, falling back to ``default`` and clamping to range.'''
    return min(max(float(raw or default), low), high)


def load_settings() -> Settings:
    '''Load settings from ``GRIMOIRE_*`` environment variables.

    ``GRIMOIRE_OPERATOR_PROMPT`` is a file path; a missing file means the built-in
    default prompt is used.
    '''
    env = os.environ.get
    return Settings(
        public_url=env('GRIMOIRE_PUBLIC_URL', 'http://localhost:8080').rstrip('/'),
        db_path=Path(env('GRIMOIRE_DB', 'data/grimoire.db')),
        title=env('GRIMOIRE_TITLE') or 'Grimoire',
        operator_prompt=_read_prompt(env('GRIMOIRE_OPERATOR_PROMPT', '')),
        ntfy=NtfyConfig(
            server=(env('GRIMOIRE_NTFY_SERVER') or 'https://ntfy.sh').rstrip('/'),
            topic=env('GRIMOIRE_NTFY_TOPIC', ''),
            token=env('GRIMOIRE_NTFY_TOKEN', ''),
        ),
        presence=PresenceConfig(
            timeout=_clamp(env('GRIMOIRE_PRESENCE_TIMEOUT'), 600, 60, 86400),
            retention_days=_clamp(env('GRIMOIRE_PRESENCE_RETENTION_DAYS'), 7, 1, 3650),
        ),
        files=FilesConfig(
            max_size=int(_clamp(env('GRIMOIRE_FILES_MAX_SIZE_MB'), 2048, 1, 2**20) * MB),
            max_total=int(_clamp(env('GRIMOIRE_FILES_MAX_TOTAL_GB'), 0, 0, 2**20) * GB),
            retention_days=_clamp(env('GRIMOIRE_FILES_RETENTION_DAYS'), 7, 1, 3650),
            large_hours=_clamp(env('GRIMOIRE_FILES_LARGE_HOURS'), 24, 1, 8760),
            huge_hours=_clamp(env('GRIMOIRE_FILES_HUGE_HOURS'), 2, 1, 8760),
        ),
    )
