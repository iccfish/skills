#!/usr/bin/env python3
"""Discover a configured NasCtl API and execute bounded, effect-aware requests."""
import argparse
import json
import os
from pathlib import Path
import re
import sys
from urllib import request
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
import uuid

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

METHODS = ('get', 'post', 'put', 'patch', 'delete', 'head', 'options')
SYNCS = {'/api/downloaders/sync-mt-tags', '/api/downloaders/sync-mount-spaces'}
POLICIES = Path(__file__).resolve().parents[1] / 'references/operations.json'


class NoRedirect(request.HTTPRedirectHandler):
    """Do not forward local authentication to another origin or login page."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def config_path(explicit=None):
    """Locate settings without depending on an agent-specific home directory."""
    override = explicit or os.environ.get('NASCTL_CONFIG')
    if override:
        return Path(override).expanduser()
    if os.environ.get('AGENT_SKILLS_CONFIG_DIR'):
        return Path(os.environ['AGENT_SKILLS_CONFIG_DIR']).expanduser() / 'nasctl.json'
    root = Path(os.environ['XDG_CONFIG_HOME']).expanduser() if os.environ.get('XDG_CONFIG_HOME') else Path.home() / '.config'
    return root / 'agent-skills/nasctl.json'


def safe_path(path):
    """Allow same-target absolute paths while blocking traversal and URL overrides."""
    if (not isinstance(path, str) or not path.startswith('/') or path.startswith('//') or
            any(c in path for c in '\\?#\r\n') or
            re.search(r'(?i)%2e|%2f|%5c', path) or
            any(part in ('.', '..') for part in path.split('/'))):
        raise SystemExit('Use an absolute API path without a query, URL override or traversal; use --query for parameters.')
    return path


def load_settings(explicit=None):
    """Resolve the local origin and optional proxy authentication without printing secrets."""
    path = config_path(explicit)
    try:
        cfg = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    except (OSError, ValueError):
        raise SystemExit('Cannot read valid local JSON configuration: %s' % path) from None
    if not isinstance(cfg, dict):
        raise SystemExit('Local configuration must be a JSON object.')
    base = os.environ.get('NASCTL_BASE') or cfg.get('base_url', '')
    if not isinstance(base, str):
        raise SystemExit('Configure a valid base_url or NASCTL_BASE.')
    parsed = urlsplit(base)
    if (any(c in base for c in '\\\r\n') or parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or
            parsed.password or parsed.query or parsed.fragment):
        raise SystemExit('Configure base_url as an HTTP(S) address without credentials, query or fragment.')
    safe_path(parsed.path or '/')
    try:
        parsed.port
    except ValueError:
        raise SystemExit('Invalid base_url port.') from None
    if cfg.get('base_url') and base.rstrip('/') != cfg['base_url'].rstrip('/') and (cfg.get('headers') or cfg.get('headers_env')):
        raise SystemExit('Use a separate --config when changing an authenticated base_url.')
    if not isinstance(cfg.get('headers', {}), dict) or not isinstance(cfg.get('headers_env', {}), dict):
        raise SystemExit('headers and headers_env must be JSON objects.')
    headers = dict(cfg.get('headers', {}))
    for name, variable in cfg.get('headers_env', {}).items():
        value = os.environ.get(variable)
        if not value:
            raise SystemExit('Required authentication environment variable is unset: %s' % variable)
        headers[name] = value
    for name, value in headers.items():
        if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9-]+', name) or
                name.lower() in ('host', 'content-length', 'transfer-encoding', 'content-type') or
                not isinstance(value, str) or any(c in value for c in '\r\n')):
            raise SystemExit('Invalid configured authentication header.')
    timeout = cfg.get('timeout_seconds', 30)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 600:
        raise SystemExit('timeout_seconds must be between 1 and 600.')
    spec_path = safe_path(cfg.get('openapi_path', '/api-docs/openapi.json'))
    return {'base_url': base.rstrip('/'), 'headers': headers, 'timeout': timeout,
            'use_proxy': cfg.get('use_proxy', False) is True, 'openapi_path': spec_path}


def http_call(settings, method, path, query=None, data=None, content_type=None):
    """Send exactly one request and retain HTTP/outcome evidence without retrying."""
    safe_path(path)
    url = settings['base_url'] + path
    if query:
        url += '?' + urlencode(query, doseq=True)
    headers = dict(settings['headers'])
    if content_type:
        headers['Content-Type'] = content_type
    headers['User-Agent'] = 'nasctl-skill/1.0'
    handlers = [NoRedirect()]
    if not settings['use_proxy']:
        handlers.append(request.ProxyHandler({}))
    req = request.Request(url, data=data, headers=headers, method=method)
    try:
        response = request.build_opener(*handlers).open(req, timeout=settings['timeout'])
    except HTTPError as exc:
        response = exc
    except (URLError, OSError):
        raise SystemExit('NasCtl connection failed; no retry performed. Reconcile state before repeating a mutation.') from None
    with response:
        raw = response.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise SystemExit('Response exceeds 16 MiB; narrow the query or page size.')
        return response.code, response.headers.get('X-Nasctl-Outcome'), response.headers.get('Content-Type', ''), raw


def openapi(settings):
    """Fetch the current contract instead of assuming the bundled route list is current."""
    status, _, _, raw = http_call(settings, 'GET', settings['openapi_path'])
    try:
        spec = json.loads(raw)
    except ValueError:
        raise SystemExit('OpenAPI endpoint returned non-JSON; check the configured path and access controls.') from None
    if status != 200 or not isinstance(spec, dict) or not isinstance(spec.get('paths'), dict):
        raise SystemExit('No valid OpenAPI contract is available; stop rather than guessing business calls.')
    return spec


def find_operation(spec, method, path):
    """Resolve a live path template, preferring literal routes over parameters."""
    safe_path(path)
    routes = spec['paths']
    for template in sorted(routes, key=lambda route: ('{' in route, route)):
        # Escape literal syntax while keeping placeholder matching explicit.
        pieces = re.split(r'(\{[^{}]+\})', template)
        pattern = ''.join('[^/{}]+' if piece.startswith('{') else re.escape(piece) for piece in pieces)
        if path == template or re.fullmatch(pattern, path):
            op = routes[template].get(method.lower())
            if isinstance(op, dict):
                return template, op
    raise SystemExit('Method/path is absent from the live contract; use operations and describe first.')


def effect(method, template, body=None):
    """Treat newly discovered operations as unreviewed, never implicitly read-only."""
    catalog = json.loads(POLICIES.read_text(encoding='utf-8'))
    item = next((row for row in catalog['operations'] if row['method'] == method.upper() and row['path'] == template), None)
    kind = item['effect'] if item else 'unreviewed'
    if kind == 'preview-or-write':
        if not isinstance(body, dict) or type(body.get('dryRun')) is not bool:
            raise SystemExit('Sync calls require an explicit JSON boolean dryRun; omission is never a preview.')
        return 'preview' if body['dryRun'] else 'write'
    return kind


def guard(method, template, body=None, allow_mutation=False):
    """Enforce reviewed effect classes and destructive-write/idempotency invariants."""
    kind = effect(method, template, body)
    if kind not in ('read', 'parse', 'preview') and not allow_mutation:
        raise SystemExit('This operation has side effects or is unreviewed; user authorization and --allow-mutation are required.')
    if template == '/api/downloaders/remove' and (not isinstance(body, dict) or type(body.get('deleteData')) is not bool):
        raise SystemExit('Torrent removal requires an explicit boolean deleteData.')
    if template == '/api/downloaders/add':
        key = body.get('idempotencyKey') if isinstance(body, dict) else None
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9._:-]{8,128}', key):
            raise SystemExit('Persist a valid idempotencyKey in the request JSON before submitting a download.')
    return kind


def classify(status, header, data):
    """Separate transport success, incomplete results and explicit business failure."""
    if not 200 <= status < 300 or header == 'failure':
        return 'failure'
    if isinstance(data, dict):
        if data.get('ok') is False or data.get('error') is True or data.get('operationStatus') == 'unknown':
            return 'failure'
        if (data.get('queueError') or any(data.get(key) for key in ('errors', 'downloaderErrors', 'writeErrors')) or
                any(isinstance(data.get(key), (int, float)) and data[key] > 0 for key in ('failed', 'skipped'))):
            return 'partial'
    return 'partial' if header == 'partial' else 'success'


def describe(spec, operation):
    """Return one operation and its reachable schemas, not the entire server manual."""
    schemas = {}

    def visit(value):
        if isinstance(value, dict):
            ref = value.get('$ref', '')
            if ref.startswith('#/components/schemas/'):
                name = ref.rsplit('/', 1)[-1]
                if name not in schemas:
                    schemas[name] = spec.get('components', {}).get('schemas', {}).get(name, {})
                    visit(schemas[name])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(operation)
    return {'operation': operation, 'schemas': schemas}


def multipart(file):
    """Encode one bounded .torrent upload; parsing does not submit a download."""
    path = Path(file)
    if not 1 <= path.stat().st_size <= 10 * 1024 * 1024:
        raise SystemExit('Torrent uploads must be between 1 byte and 10 MiB.')
    boundary = 'nasctl-' + uuid.uuid4().hex
    filename = path.name.replace('"', '_').replace('\r', '_').replace('\n', '_')
    head = ('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\nContent-Type: application/x-bittorrent\r\n\r\n' % (boundary, filename)).encode('utf-8')
    return head + path.read_bytes() + ('\r\n--%s--\r\n' % boundary).encode(), 'multipart/form-data; boundary=' + boundary


def emit(value, settings):
    """Redact locally configured authentication values from displayed JSON."""
    text = json.dumps(value, ensure_ascii=False, indent=2)
    for secret in settings['headers'].values():
        if secret:
            text = text.replace(secret, '<redacted>')
    print(text)


def main():
    """Discover operations, inspect schemas, or invoke an explicitly selected API."""
    parser = argparse.ArgumentParser(description=__doc__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--config')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('operations', parents=[common])
    for command in ('describe', 'request'):
        sub = commands.add_parser(command, parents=[common])
        sub.add_argument('--method', default='GET', choices=[method.upper() for method in METHODS])
        sub.add_argument('--path', required=True)
        if command == 'request':
            sub.add_argument('--query', action='append', default=[], metavar='NAME=VALUE')
            inputs = sub.add_mutually_exclusive_group()
            inputs.add_argument('--body-file')
            inputs.add_argument('--upload-file')
            sub.add_argument('--allow-mutation', action='store_true')
            sub.add_argument('--out-file')
    args = parser.parse_args()
    settings = load_settings(args.config)
    spec = openapi(settings)
    if args.command == 'operations':
        rows = [{'method': method.upper(), 'path': path, 'summary': op.get('summary'),
                 'effect': 'preview-or-write' if path in SYNCS and method == 'post' else effect(method, path)}
                for path, methods in spec['paths'].items() for method, op in methods.items() if method in METHODS]
        emit({'api': spec.get('info'), 'operations': rows}, settings)
        return
    template, operation = find_operation(spec, args.method, args.path)
    if args.command == 'describe':
        emit(describe(spec, operation), settings)
        return
    if '{' in args.path or '}' in args.path:
        raise SystemExit('Replace path placeholders with IDs discovered from current responses.')
    query = []
    for pair in args.query:
        if '=' not in pair:
            raise SystemExit('--query expects NAME=VALUE.')
        query.append(pair.split('=', 1))
    body = None
    if args.body_file:
        try:
            body = json.loads(Path(args.body_file).read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            raise SystemExit('Invalid UTF-8 JSON body file.') from None
    guard(args.method, template, body, args.allow_mutation)
    if args.method in ('GET', 'HEAD') and (args.body_file or args.upload_file):
        raise SystemExit('GET/HEAD requests cannot include a body.')
    raw_body, media_type = None, None
    if args.upload_file:
        if args.method != 'POST' or template != '/api/downloaders/torrent-file/parse':
            raise SystemExit('--upload-file is only supported for torrent-file/parse.')
        raw_body, media_type = multipart(args.upload_file)
    elif args.body_file:
        raw_body, media_type = json.dumps(body, ensure_ascii=False).encode('utf-8'), 'application/json'
    status, header, content_type, raw = http_call(settings, args.method, args.path, query, raw_body, media_type)
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        if not args.out_file:
            raise SystemExit('Non-JSON response; use --out-file for expected binary results. Do not repeat mutations blindly.') from None
        data = None
    outcome = classify(status, header, data)
    if args.out_file and outcome != 'failure':
        Path(args.out_file).write_bytes(raw)
        data = {'saved': args.out_file, 'bytes': len(raw), 'contentType': content_type}
    emit({'httpStatus': status, 'outcome': outcome, 'data': data}, settings)
    raise SystemExit({'success': 0, 'partial': 2, 'failure': 1}[outcome])


if __name__ == '__main__':
    main()
