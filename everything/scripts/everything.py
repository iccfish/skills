#!/usr/bin/env python3
"""Windows Everything instance management and read-only HTTP/ES search (stdlib)."""
import argparse
import base64
import configparser
import csv
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib import error, parse, request


class SkillError(Exception):
    def __init__(self, message, guidance=None):
        super().__init__(message)
        self.guidance = guidance or []


def installation_guidance(code):
    downloads = {'everything': 'https://www.voidtools.com/downloads/',
                 'http_plugin': 'https://github.com/voidtools/http_server/releases',
                 'es': 'https://github.com/voidtools/ES/releases'}
    summaries = {
        'everything_not_discovered': (
            '未发现本机 Everything 配置；可能尚未安装、未启动或配置不可见。',
            '若已安装，先启动 Everything 并执行 discover；否则从官方下载页选择匹配系统架构的非 Lite 版本，安装或解压后启动，等待索引建立，再发现实例。',
            ('everything',)),
        'http_setup_required': (
            '已发现 Everything，但尚未确认可用的 HTTP 插件服务；请结合 diagnostics 检查配置与连接状态。',
            '未安装插件时，Everything 1.5 下载匹配主程序架构的 HTTP Server 插件，运行安装器添加，或解压 DLL 到安装目录的 plugins 子目录后重启。已安装但未启用时，打开工具→选项→HTTP 服务器，启用并设置端口及用户名/密码，再执行 discover；连接失败先检查端口与认证，无需直接重装。Everything 1.4 标准版内置 HTTP，无需插件，启用后用 add 手工配置 HTTP 实例。仅本机检索可直接使用 ES。',
            ('http_plugin', 'everything')),
        'es_not_found': (
            '已发现 Everything，但在本次发现目录中未找到 es.exe。',
            '从官方 ES 发布页下载匹配 Windows 架构的 ZIP（本技能要求 1.1.0.37+），解压 es.exe 到 Everything 安装目录；保持 Everything 正在运行，再执行 discover。放在其他目录时用 add 指定本机 es_path；ES 只能查询本机 Everything。',
            ('es',)),
    }
    summary, overview, links = summaries[code]
    return {'code': code, 'summary': summary, 'overview': overview,
            'downloads': {key: downloads[key] for key in links}}


def require_windows():
    if sys.platform != 'win32':
        raise SkillError('暂不支持非 Windows 平台。')


def config_path(explicit=None):
    if explicit or os.environ.get('EVERYTHING_CONFIG'):
        return Path(explicit or os.environ['EVERYTHING_CONFIG']).expanduser()
    if os.environ.get('AGENT_SKILLS_CONFIG_DIR'):
        return Path(os.environ['AGENT_SKILLS_CONFIG_DIR']) / 'everything.json'
    return Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'agent-skills/everything.json'


def validate_instance(item):
    if not isinstance(item, dict) or not re.fullmatch(r'[A-Za-z0-9_.-]+', str(item.get('id', ''))):
        raise SkillError('实例 id 只能包含字母、数字、下划线、点和短横线。')
    if item.get('mode') not in ('http', 'es'):
        raise SkillError('mode 必须为 http 或 es。')
    timeout = item.get('timeout_seconds', 10)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 1 <= timeout <= 600:
        raise SkillError('timeout_seconds 必须在 1–600 之间。')
    for key in ('username', 'password', 'username_env', 'password_env', 'name', 'instance_name'):
        if key in item and not isinstance(item[key], str):
            raise SkillError(f'{key} 必须为字符串。')
    if item['mode'] == 'http':
        try:
            url = parse.urlsplit(item.get('base_url', ''))
            _ = url.port
        except (ValueError, TypeError):
            raise SkillError('HTTP 地址无效。') from None
        if url.scheme not in ('http', 'https') or not url.hostname or url.username is not None or url.password is not None or url.query or url.fragment:
            raise SkillError('base_url 必须为不含凭据、查询和片段的 HTTP(S) 地址。')
    else:
        endpoint = item.get('es_path', '')
        if not isinstance(endpoint, str) or not Path(endpoint).is_absolute() or endpoint.startswith(('\\\\', '//')) or re.match(r'^[A-Za-z]:[\\/]', endpoint) is None:
            raise SkillError('ES 只支持本机盘符下的绝对 es.exe 路径，不支持远程地址或 UNC。')
        if Path(endpoint).name.lower() != 'es.exe':
            raise SkillError('es_path 必须指向 es.exe，不能使用 Everything.exe。')
        if item.get('base_url') or item.get('host') or item.get('port'):
            raise SkillError('ES 仅查询本机 Everything；远程实例请使用 HTTP。')
    return item


def load_config(path):
    if not path.exists():
        return {'version': 1, 'instances': []}
    cfg = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(cfg, dict) or cfg.get('version') != 1 or not isinstance(cfg.get('instances'), list):
        raise SkillError('配置须为 version:1 和 instances 数组。')
    ids = set()
    for item in cfg['instances']:
        validate_instance(item)
        if item['id'] in ids:
            raise SkillError('配置包含重复实例 id。')
        ids.add(item['id'])
    if cfg.get('default_instance') and cfg['default_instance'] not in ids:
        raise SkillError('default_instance 不存在。')
    return cfg


def save_config(path, cfg):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.everything-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(cfg, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def public_instance(item):
    return {**{k: v for k, v in item.items() if k not in ('password', 'username')},
            'has_username': bool(item.get('username') or item.get('username_env')),
            'has_password': bool(item.get('password') or item.get('password_env'))}


def credential(item, key):
    env = item.get(key + '_env')
    if env:
        if env not in os.environ:
            raise SkillError(f'缺少凭据环境变量：{env}')
        return os.environ[env]
    return item.get(key, '')


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_search(item, query, limit=100, offset=0, sort='name', descending=False,
                regex=False, case=False, whole_word=False, match_path=False, diacritics=False):
    if sort not in ('name', 'path', 'size', 'date-modified'):
        raise SkillError('HTTP 排序仅支持 name、path、size、date-modified。')
    params = {'search': query, 'json': 1, 'count': limit, 'offset': offset,
              'sort': sort.replace('-', '_'), 'ascending': int(not descending), 'regex': int(regex),
              'diacritics': int(diacritics),
              'case': int(case), 'wholeword': int(whole_word), 'path': int(match_path),
              'path_column': 1, 'size_column': 1, 'date_modified_column': 1,
              'date_created_column': 1, 'attributes_column': 1}
    headers = {'Accept': 'application/json'}
    username, password = credential(item, 'username'), credential(item, 'password')
    if ':' in username:
        raise SkillError('HTTP Basic 用户名不能包含冒号。')
    if username or password:
        token = base64.b64encode(f'{username}:{password}'.encode('utf-8')).decode('ascii')
        headers['Authorization'] = 'Basic ' + token
    url = item['base_url'].rstrip('/') + '/?' + parse.urlencode(params)
    opener = request.build_opener(request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request.Request(url, headers=headers), timeout=item.get('timeout_seconds', 10)) as response:
            raw = response.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise SkillError('HTTP 响应超过 16 MiB；请减小分页大小。')
        data = json.loads(raw.decode('utf-8-sig'))
    except error.HTTPError as exc:
        raise SkillError(f'HTTP 请求失败（状态 {exc.code}）。检查地址、认证和 JSON 支持。') from None
    except (error.URLError, TimeoutError, OSError):
        raise SkillError('HTTP 连接失败或超时；检查实例地址与服务状态。') from None
    except (ValueError, UnicodeError):
        raise SkillError('接口未返回有效 JSON；请确认插件兼容 Everything HTTP JSON API。') from None
    if (not isinstance(data, dict) or not isinstance(data.get('results'), list)
            or isinstance(data.get('totalResults'), bool) or not isinstance(data.get('totalResults'), int)
            or data['totalResults'] < 0 or any(not isinstance(x, dict) for x in data['results'])):
        raise SkillError('接口响应不符合 Everything JSON 结果结构。')
    return {'instance': item['id'], 'mode': 'http', 'offset': offset,
            'total': data['totalResults'], 'returned': len(data['results']), 'results': data['results']}


def run_es(argv, timeout):
    try:
        completed = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   timeout=timeout, shell=False, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.TimeoutExpired):
        raise SkillError('ES 启动失败或超时；检查 es.exe 与本机 Everything 是否运行。') from None
    if completed.returncode:
        # ES output may contain user queries/paths; do not reflect it in errors.
        raise SkillError(f'ES 查询失败（退出码 {completed.returncode}）；检查本机 IPC、ES 版本和权限。')
    return completed


def es_search(item, query, limit=100, offset=0, sort='name', descending=False,
              regex=False, case=False, whole_word=False, match_path=False, diacritics=False):
    if not Path(item['es_path']).is_file():
        raise SkillError('未找到本机 es.exe。')
    version = run_es([item['es_path'], '-version'], item.get('timeout_seconds', 10))
    numbers = re.search(rb'(\d+)\.(\d+)\.(\d+)\.(\d+)', version.stdout)
    es_version = tuple(int(x) for x in numbers.groups()) if numbers else ()
    if es_version < (1, 1, 0, 37):
        raise SkillError('请使用 ES 1.1.0.37 或更新版本（需要 -argv 正确传递检索式）。')
    if query.lstrip().startswith(('-', '/')):
        raise SkillError('ES 检索式不能以选项前缀开头；用双引号包围该文件名，或改用 HTTP。')
    with tempfile.TemporaryDirectory(prefix='everything-es-') as folder:
        target = Path(folder) / 'results.csv'
        paging = (['-n', str(offset + limit), '-viewport-offset', str(offset), '-viewport-count', str(limit)]
                  if es_version >= (1, 1, 0, 38) else ['-n', str(limit), '-offset', str(offset)])
        argv = [item['es_path'], '-argv', *paging, '-sort', sort,
                '-sort-descending' if descending else '-sort-ascending', '-name', '-path-column',
                '-size', '-date-created', '-date-modified', '-attributes', '-utf8-bom',
                '-size-format', '1', '-date-format', '1', '-no-pause', '-no-highlight',
                '-no-hide-empty-search-results', '-no-empty-search-help',
                '-timeout', str(max(1, int(item.get('timeout_seconds', 10) * 1000) - 500)),
                '-export-csv', str(target)]
        if item.get('instance_name'):
            argv += ['-instance', item['instance_name']]
        for enabled, flag in ((case, 'case'), (whole_word, 'whole-word'), (match_path, 'match-path'), (diacritics, 'diacritics')):
            argv.append(('-' if enabled else '-no-') + flag)
        # ES -argv uses CommandLineToArgvW, matching subprocess Windows quoting.
        # The entire query is one non-option argument, including embedded quotes.
        if regex:
            argv += ['-regex', query]
        elif query:
            argv.append(query)
        run_es(argv, item.get('timeout_seconds', 10))
        if not target.is_file():
            raise SkillError('ES 没有生成 CSV；请更新支持所用选项的 ES。')
        with target.open(encoding='utf-8-sig', newline='') as stream:
            results = list(csv.DictReader(stream))
    return {'instance': item['id'], 'mode': 'es', 'offset': offset,
            'total': None, 'returned': len(results), 'results': results}


def search(item, query, **options):
    validate_instance(item)
    return (http_search if item['mode'] == 'http' else es_search)(item, query, **options)


def read_ini(path):
    raw = path.read_bytes()
    if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
        text = raw.decode('utf-16')
    else:
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = raw.decode('mbcs')
    # Some Everything/Plugin INIs have root keys without a section.
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.read_string('[__root__]\n' + text)
    return parser


def ini_value(parser, key):
    for section in parser.sections():
        if parser.has_option(section, key):
            return parser.get(section, key).strip().strip('"')
    return ''


def process_paths():
    script = ("[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); "
              "$p = @(Get-CimInstance Win32_Process -ErrorAction Stop | "
              "Where-Object { $_.Name -in @('Everything.exe','Everything64.exe','Everythin64g.exe') } | "
              "Select-Object -ExpandProperty ExecutablePath); ConvertTo-Json -InputObject $p -Compress")
    try:
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                                capture_output=True, timeout=15, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            return []
        return [Path(p) for p in json.loads(result.stdout.decode('utf-8-sig') or '[]') if p]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []


def find_inis():
    appdata = os.environ.get('APPDATA')
    if appdata:
        folder = Path(appdata) / 'Everything'
        if folder.exists():
            found = sorted(folder.rglob('Everything.ini'))
            if found:
                return found, []
    executables = process_paths()
    return list(dict.fromkeys(p.parent / 'Everything.ini' for p in executables if (p.parent / 'Everything.ini').is_file())), executables


def discover(cfg):
    inis, executables = find_inis()
    if not inis:
        return {'added': [], 'diagnostics': ['未找到 Everything.ini，跳过默认实例添加。'],
                'guidance': [installation_guidance('everything_not_discovered')]}
    added, diagnostics = [], []
    readable_ini, http_available, es_found = False, False, False
    live = executables or process_paths()
    for ini in inis:
        try:
            parsed = read_ini(ini)
            readable_ini = True
            # APPDATA INI can live separately from the installation. Inspect process
            # paths only to locate the matching installation's ES/Plugin files.
            candidates = [ini.parent]
            candidates.extend(p.parent for p in live)
            candidates = list(dict.fromkeys(candidates))
            if 'http_server' in ini_value(parsed, 'plugin_filename').lower():
                for folder in [ini.parent]:
                    plugin = folder / 'Plugin.ini'
                    if not plugin.is_file():
                        plugin = folder / 'Plugins.ini'
                        if not plugin.is_file():
                            continue
                    try:
                        plugin_cfg = read_ini(plugin)
                        section = next((s for s in plugin_cfg.sections() if s.lower() == 'http_server64.dll'), None)
                        if not section:
                            continue
                        port = plugin_cfg.getint(section, 'port')
                        if not 1 <= port <= 65535:
                            raise ValueError('port')
                    except (OSError, ValueError, configparser.Error, UnicodeError):
                        diagnostics.append('HTTP 插件配置无法读取或端口无效；继续检查 ES。')
                        continue
                    item = {'id': 'local-http', 'mode': 'http', 'base_url': f'http://127.0.0.1:{port}',
                            'username': plugin_cfg.get(section, 'username', fallback='').strip('"'),
                            'password': plugin_cfg.get(section, 'password', fallback='').strip('"')}
                    http_available = try_register(cfg, item, added, diagnostics) or http_available
            for folder in candidates:
                es = folder / 'es.exe'
                if es.is_file():
                    es_found = True
                    item = {'id': 'local-es', 'mode': 'es', 'es_path': str(es.resolve())}
                    instance_name = ini_value(parsed, 'instance_name')
                    if instance_name:
                        item['instance_name'] = instance_name
                    try_register(cfg, item, added, diagnostics)
        except (OSError, ValueError, configparser.Error, UnicodeError):
            diagnostics.append('发现的 INI 无法读取或配置无效；已跳过。')
    if not added:
        diagnostics.append('没有新增可用本机实例；可手动添加实例。')
    guidance = []
    if readable_ini and not http_available:
        guidance.append(installation_guidance('http_setup_required'))
    if readable_ini and not es_found:
        guidance.append(installation_guidance('es_not_found'))
    return {'added': added, 'diagnostics': diagnostics, 'guidance': guidance}


def try_register(cfg, item, added, diagnostics):
    endpoint = 'base_url' if item['mode'] == 'http' else 'es_path'
    if any(x['mode'] == item['mode'] and x.get(endpoint) == item[endpoint]
           and x.get('instance_name', '') == item.get('instance_name', '') for x in cfg['instances']):
        return True
    root, number = item['id'], 2
    while any(x['id'] == item['id'] for x in cfg['instances']):
        item['id'] = f'{root}-{number}'
        number += 1
    try:
        search(item, '', limit=1)
    except SkillError as exc:
        diagnostics.append(f'{item["mode"]} 本机实例验证失败：{exc}')
        return False
    cfg['instances'].append(item)
    added.append(public_instance(item))
    return True


def parser():
    root = argparse.ArgumentParser(description='Windows Everything 实例管理与文件信息查询')
    root.add_argument('--config', help='本地 JSON 配置路径（放在子命令前）')
    commands = root.add_subparsers(dest='command', required=True)
    commands.add_parser('list', help='列出实例；空配置自动发现本机')
    commands.add_parser('discover', help='发现并验证本机 HTTP/ES 实例')
    for action in ('add', 'update'):
        p = commands.add_parser(action, help='通过 JSON 文件添加/修改实例')
        p.add_argument('--file', required=True, help='单个实例 JSON；update 按 id 合并字段')
        p.add_argument('--default', action='store_true')
    p = commands.add_parser('remove', help='删除实例')
    p.add_argument('id')
    p = commands.add_parser('search', help='只读查询文件索引')
    p.add_argument('--instance')
    p.add_argument('--query', default='')
    p.add_argument('--query-file', help='从 UTF-8 文件读取原始检索式，与 --query 互斥')
    p.add_argument('--limit', type=int, default=100)
    p.add_argument('--offset', type=int, default=0)
    p.add_argument('--sort', choices=('name', 'path', 'size', 'extension', 'date-created', 'date-modified', 'attributes'), default='name')
    for flag in ('descending', 'regex', 'case', 'whole-word', 'match-path', 'diacritics'):
        p.add_argument('--' + flag, action='store_true')
    return root


def main(argv=None):
    require_windows()
    args = parser().parse_args(argv)
    path = config_path(args.config)
    cfg = load_config(path)
    discovery = None
    # remove/update must not rediscover and undo explicit instance management.
    if args.command == 'discover' or (not cfg['instances'] and args.command in ('list', 'search')):
        discovery = discover(cfg)
        if discovery['added']:
            save_config(path, cfg)
    if args.command in ('list', 'discover'):
        return {'instances': [public_instance(x) for x in cfg['instances']],
                'default_instance': cfg.get('default_instance'), 'discovery': discovery}
    if args.command in ('add', 'update'):
        item = json.loads(Path(args.file).read_text(encoding='utf-8-sig'))
        if not isinstance(item, dict):
            raise SkillError('实例文件必须为 JSON 对象。')
        old = next((x for x in cfg['instances'] if x['id'] == item.get('id')), None)
        if args.command == 'add' and old:
            raise SkillError('实例 id 已存在；请使用 update。')
        if args.command == 'update' and not old:
            raise SkillError('实例不存在；请使用 add。')
        updated = validate_instance({**(old or {}), **item})
        if old:
            cfg['instances'][cfg['instances'].index(old)] = updated
        else:
            cfg['instances'].append(updated)
        if args.default:
            cfg['default_instance'] = updated['id']
        save_config(path, cfg)
        return {'saved': public_instance(updated)}
    if args.command == 'remove':
        remaining = [x for x in cfg['instances'] if x['id'] != args.id]
        if len(remaining) == len(cfg['instances']):
            raise SkillError('实例不存在。')
        cfg['instances'] = remaining
        if cfg.get('default_instance') == args.id:
            cfg.pop('default_instance')
        save_config(path, cfg)
        return {'removed': args.id}
    if not cfg['instances']:
        raise SkillError('没有可用的 Everything 实例，请使用 add 添加实例，或启动本机 Everything 后执行 discover。',
                         guidance=discovery.get('guidance', []) if discovery else [])
    chosen = args.instance or cfg.get('default_instance')
    if not chosen and len(cfg['instances']) > 1:
        raise SkillError('存在多个实例，请用 --instance 指定，或设置 default_instance。')
    item = next((x for x in cfg['instances'] if x['id'] == chosen), None) if chosen else cfg['instances'][0]
    if not item:
        raise SkillError('指定的实例不存在，请先 list。')
    if not 1 <= args.limit <= 10000 or args.offset < 0:
        raise SkillError('limit 必须为 1–10000，offset 不能为负数。')
    if args.query_file and args.query:
        raise SkillError('--query 与 --query-file 不能同时使用。')
    query = Path(args.query_file).read_text(encoding='utf-8-sig') if args.query_file else args.query
    if '\x00' in query:
        raise SkillError('检索式不能包含 NUL。')
    return search(item, query, limit=args.limit, offset=args.offset, sort=args.sort,
                  descending=args.descending, regex=args.regex, case=args.case,
                  whole_word=args.whole_word, match_path=args.match_path, diacritics=args.diacritics)


if __name__ == '__main__':
    try:
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8')
        print(json.dumps(main(), ensure_ascii=False, indent=2))
    except (SkillError, OSError, ValueError, configparser.Error) as exc:
        message = str(exc) if isinstance(exc, SkillError) else '配置或输入文件无法读取，请检查路径、编码与 JSON/INI 格式。'
        payload = {'error': message}
        if isinstance(exc, SkillError) and exc.guidance:
            payload['guidance'] = exc.guidance
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
