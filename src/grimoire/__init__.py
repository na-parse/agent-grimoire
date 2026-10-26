'''LAN-local agent coordination board with ntfy operator notices.'''

from .app import create_app
from .config import Settings, load_settings

__all__ = ['Settings', 'create_app', 'load_settings']
