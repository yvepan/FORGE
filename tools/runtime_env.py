"""Load authorized local settings without evaluating shell code."""
import os
from pathlib import Path

ALLOWED = {'FORGE_API_KEY', 'FORGE_API_ORIGIN', 'HTTP_PROXY', 'HTTPS_PROXY',
           'NO_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy',
           'FORGE_GATEWAY_RETRIES'}


def load_env(path):
    path = Path(path)
    if not path.exists():
        return
    for raw in path.read_text(encoding='utf-8-sig').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        if line.startswith('$env:'):
            line = line[5:]
        key, value = line.split('=', 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if key in ALLOWED:
            os.environ[key] = value
