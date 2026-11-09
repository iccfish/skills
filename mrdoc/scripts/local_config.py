"""Agent-independent local settings and secret resolution; never prints secrets."""
import json
import os
from pathlib import Path
import shutil
import subprocess
from urllib.parse import urlsplit


def config_path(skill, explicit=None):
    """Resolve a machine-local configuration path independently of the agent host."""
    specific = {'mrdoc': 'MRDOC_CONFIG', 'portainer': 'PORTAINER_CONFIG'}[skill]
    override = explicit or os.environ.get(specific)
    if override:
        return Path(override).expanduser()
    directory = os.environ.get('AGENT_SKILLS_CONFIG_DIR')
    if directory:
        return Path(directory).expanduser() / (skill + '.json')
    base = Path(os.environ['XDG_CONFIG_HOME']).expanduser() if os.environ.get('XDG_CONFIG_HOME') else Path.home() / '.config'
    return base / 'agent-skills' / (skill + '.json')


def load_config(skill, explicit=None):
    """Load an optional JSON object; malformed settings fail without revealing text."""
    path = config_path(skill, explicit)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
    except (ValueError, OSError):
        raise SystemExit('Cannot read valid JSON configuration: %s' % path) from None
    if not isinstance(data, dict):
        raise SystemExit('Configuration must be a JSON object: %s' % path)
    return data


def validate_origin(value):
    """Restrict credential destinations to explicit HTTP(S) origins."""
    origin = urlsplit(value or '')
    if (origin.scheme not in ('http', 'https') or not origin.hostname or
            origin.username or origin.password or origin.path not in ('', '/') or
            origin.query or origin.fragment or any(c in value for c in '\r\n\\')):
        raise SystemExit('Configure a base_url containing only an HTTP(S) origin.')
    try:
        origin.port
    except ValueError:
        raise SystemExit('Invalid base_url port.') from None
    return value.rstrip('/')


def resolve_secret(entry, field, override_env=None):
    """Prefer environment secrets; retain optional Windows DPAPI for existing users."""
    if override_env and os.environ.get(override_env):
        value = os.environ[override_env]
    elif entry.get(field + '_env'):
        name = entry[field + '_env']
        value = os.environ.get(name)
        if not value:
            raise SystemExit('Required secret environment variable is unset: %s' % name)
    elif entry.get(field):
        value = entry[field]
    elif entry.get(field + '_dpapi'):
        runtime = shutil.which('pwsh') or shutil.which('powershell')
        if os.name != 'nt' or not runtime:
            raise SystemExit('DPAPI needs its original Windows user and PowerShell; configure an environment secret on this machine.')
        code = "$ErrorActionPreference='Stop'; $s=ConvertTo-SecureString ([Console]::In.ReadToEnd()); [Console]::Out.Write([Net.NetworkCredential]::new('', $s).Password)"
        result = subprocess.run([runtime, '-NoProfile', '-NonInteractive', '-Command', code],
                                input=entry[field + '_dpapi'], text=True, capture_output=True, timeout=20)
        if result.returncode:
            raise SystemExit('Cannot decrypt local DPAPI credential; reconfigure this machine.')
        value = result.stdout
    else:
        raise SystemExit('Missing %s; configure a local secret or its environment variable.' % field)
    if not isinstance(value, str) or not value.strip() or '\r' in value or '\n' in value:
        raise SystemExit('Invalid %s credential.' % field)
    return value
