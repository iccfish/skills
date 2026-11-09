#!/usr/bin/env python3
"""Portable Portainer API client with local credentials and explicit write gating."""
import argparse
import json
import os
from pathlib import Path
import re
import sys
from urllib import request
from urllib.error import HTTPError, URLError
from local_config import config_path, load_config, resolve_secret, validate_origin

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')


class NoRedirect(request.HTTPRedirectHandler):
    """Do not forward API credentials to redirect targets."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_path(path):
    """Keep API calls within the configured origin and disallow traversal."""
    pathname = path.split('?', 1)[0]
    if (not path.startswith('/api/') or any(c in path for c in '\\#\r\n') or
            re.search(r'(?i)%2e|%2f|%5c', pathname) or
            any(segment in ('.', '..') for segment in pathname.split('/'))):
        raise SystemExit('Use an /api/ path without traversal, fragments or encoded path separators.')
    return path


def decode_logs(data):
    """Decode Docker multiplex frames; keep unframed TTY logs readable."""
    offset, chunks = 0, []
    while offset + 8 <= len(data) and data[offset] in (0, 1, 2) and data[offset + 1:offset + 4] == b'\0\0\0':
        size = int.from_bytes(data[offset + 4:offset + 8], 'big')
        end = offset + 8 + size
        if end > len(data):
            raise SystemExit('Incomplete Docker log frame.')
        chunks.append(data[offset + 8:end])
        offset = end
    if offset and offset != len(data):
        raise SystemExit('Unexpected trailing Docker log bytes.')
    return (b''.join(chunks) if offset else data).decode('utf-8', 'replace')


def select_instance(cfg, name):
    """Select a configured alias without assuming a deployment or endpoint ID."""
    instances = cfg.get('instances', {})
    if not isinstance(instances, dict):
        raise SystemExit('instances must be a JSON object.')
    chosen = name or cfg.get('default_instance')
    if not chosen and len(instances) == 1:
        chosen = next(iter(instances))
    if chosen not in instances:
        raise SystemExit('Choose an instance using instances and --instance, or configure default_instance.')
    return chosen, instances[chosen]


def api_request(entry, path, method='GET', body=None, allow_mutation=False, timeout=30, use_proxy=False):
    """Send one bounded request; never retry writes or expose error payloads."""
    validate_path(path)
    if method not in ('GET', 'HEAD') and not allow_mutation:
        raise SystemExit('Write requests require user authorization and --allow-mutation.')
    if body is not None and method in ('GET', 'HEAD'):
        raise SystemExit('Read requests cannot have a body.')
    origin = validate_origin(entry.get('base_url'))
    key = resolve_secret(entry, 'api_key', 'PORTAINER_API_KEY')
    headers = {'X-API-Key': key, 'User-Agent': 'portainer-skill/2.0'}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode('utf-8')
        headers['Content-Type'] = 'application/json; charset=utf-8'
    handlers = [NoRedirect()]
    if not use_proxy:
        handlers.append(request.ProxyHandler({}))
    opener = request.build_opener(*handlers)
    req = request.Request(origin + path, data=data, headers=headers, method=method)
    try:
        with opener.open(req, timeout=timeout) as response:
            return response.status, response.headers.get('Content-Type', ''), response.read()
    except HTTPError as exc:
        status = exc.code
        exc.close()
        raise SystemExit('Portainer returned HTTP %s for %s %s. No retry performed.' % (status, method, path.split('?', 1)[0])) from None
    except (URLError, OSError):
        raise SystemExit('Portainer connection failed. Check reachability; reconcile state before retrying writes.') from None


def build_parser():
    """Expose portable metadata discovery and generic Portainer API requests."""
    parser = argparse.ArgumentParser(description=__doc__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--config', help='Local JSON configuration file')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('instances', parents=[common], help='List configured aliases and origins, never credentials')
    api = sub.add_parser('request', parents=[common], help='Call a Portainer /api/ path')
    api.add_argument('--instance')
    api.add_argument('--path', required=True)
    api.add_argument('--method', choices=['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE'], default='GET')
    api.add_argument('--body-file', help='UTF-8 JSON request body')
    output = api.add_mutually_exclusive_group()
    output.add_argument('--out-file', help='Save raw response bytes')
    output.add_argument('--docker-logs', action='store_true', help='Decode Docker log frames')
    api.add_argument('--allow-mutation', action='store_true')
    api.add_argument('--timeout', type=int, default=30)
    api.add_argument('--use-proxy', action='store_true', help='Opt in to system proxy settings')
    return parser


def main():
    """Load local settings and format only the explicitly requested response."""
    args = build_parser().parse_args()
    cfg = load_config('portainer', args.config)
    if args.command == 'instances':
        rows = [{'name': name, 'base_url': entry.get('base_url'), 'description': entry.get('description', '')}
                for name, entry in cfg.get('instances', {}).items()]
        print(json.dumps({'config_path': str(config_path('portainer', args.config)), 'instances': rows}, ensure_ascii=False, indent=2))
        return
    if not 1 <= args.timeout <= 600:
        raise SystemExit('--timeout must be between 1 and 600 seconds.')
    _, entry = select_instance(cfg, args.instance)
    body = None
    if args.body_file:
        try:
            body = json.loads(Path(args.body_file).read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            raise SystemExit('Cannot read valid UTF-8 JSON from --body-file.') from None
    status, content_type, data = api_request(entry, args.path, args.method, body,
                                            args.allow_mutation, args.timeout, args.use_proxy)
    if args.out_file:
        Path(args.out_file).write_bytes(data)
    elif args.docker_logs:
        print(decode_logs(data))
    elif not data:
        print('HTTP %s' % status)
    elif not content_type or any(kind in content_type for kind in ('json', 'text', 'xml')):
        print(data.decode('utf-8', 'replace'))
    else:
        raise SystemExit('Binary response: use --out-file or --docker-logs.')


if __name__ == '__main__':
    main()
