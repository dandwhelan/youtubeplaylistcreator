"""Local storage for API keys, shared by the CLI and the web UI.

Lookup order for every key: environment variable first, then settings.json
(written by the web UI's "API keys" panel). Existing env-var setups keep
working unchanged; settings.json is a plaintext local file and must not be
committed (it's in .gitignore, like token.json).
"""
import json
import os

SETTINGS_FILE = 'settings.json'

# Keys the web UI is allowed to read/write.
MANAGED_KEYS = ('SETLIST_FM_API_KEY', 'GEMINI_API_KEY')


def load_settings():
    if not os.path.exists(SETTINGS_FILE):
        return {}
    try:
        with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, IOError):
        return {}


def save_settings(settings):
    with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(settings, f, indent=2)


def get_key(name):
    """Return the key value: environment first, then settings.json."""
    return os.environ.get(name, '') or load_settings().get(name, '')


def key_source(name):
    """Where the key comes from: 'env', 'saved', or None if unset."""
    if os.environ.get(name, ''):
        return 'env'
    if load_settings().get(name, ''):
        return 'saved'
    return None


def set_key(name, value):
    """Save (or clear, if value is empty) a key in settings.json."""
    settings = load_settings()
    value = (value or '').strip()
    if value:
        settings[name] = value
    else:
        settings.pop(name, None)
    save_settings(settings)
