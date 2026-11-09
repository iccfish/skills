"""Render offline minutes from verified final attribution and agent-authored grounded content."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
from pathlib import Path
from string import Template
from datetime import datetime, timezone
from urllib.parse import quote

import edits
import review
from localio import read_json, atomic_text, atomic_json, file_lock

ASSETS = Path(__file__).resolve().parent.parent / 'assets'


def sha(path: Path) -> str:
    """Bind a report to exact local source bytes without copying credentials or vectors."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def context(raw: Path) -> dict:
    """Replay final corrections and require resolved sampling before the final report stage."""
    raw = raw.resolve()
    if not raw.name.endswith('.asr.json'):
        raise ValueError('请指定 .asr.json 原始结果')
    paths = review.artifact_paths(raw)
    meta_path = raw.with_name(raw.name[:-len('.asr.json')] + '.meta.json')
    meta = read_json(meta_path)
    workflow = meta.get('workflow', {})
    if workflow.get('stage') != 'separated':
        raise ValueError('请先完成归属校准并明确继续，生成最终分离稿及采样')
    raw_rows = read_json(raw)['transcripts'][0]['sentences']
    initial = read_json(paths['attribution'])
    if initial.get('raw_revision') != review.fingerprint(raw_rows) or review.fingerprint(initial) != workflow.get('attribution_revision'):
        raise ValueError('首次归属或原始结果已变化，请先核对并重新完成校准流程')
    initial_names = {int(k): v for k, v in initial['names'].items()}
    frozen_text = review.render(raw_rows, initial_names, '归属转写稿未确认版（首次归属快照）')
    if hashlib.sha256(frozen_text.encode()).hexdigest() != initial.get('text_sha256') or paths['attributed'].read_text(encoding='utf-8') != frozen_text:
        raise ValueError('未确认版与首次快照不一致，请先核对')
    rows = edits.tag_origins(raw_rows)
    result = edits.apply(rows, edits.load(meta_path))
    if any(result[k] for k in ('skipped', 'warnings', 'conflicts')):
        raise ValueError('仍有未解决的人工调整，请先完成校准')
    manual = dict(result['names'])
    manual = {k: v for k, v in manual.items() if v != f'说话人{k + 1}'}
    revision = review.fingerprint([rows, manual, initial_names])
    if revision != workflow.get('review_revision'):
        raise ValueError('校准记录已变化，请重新继续处理并确认采样')
    names = {r['speaker_id']: manual.get(r['speaker_id'], initial_names.get(r['speaker_id'],
             f"说话人{r['speaker_id'] + 1}" if isinstance(r['speaker_id'],int) else '未分配')) for r in rows}
    expected = review.render(rows, names, '说话人分离（姓名建议须试听采样确认）')
    if paths['separated'].read_text(encoding='utf-8') != expected:
        raise ValueError('最终分离稿与校准记录不一致，请先核对')
    if paths['reviewed'].exists() and paths['reviewed'].read_text(encoding='utf-8') != review.render(rows, names, '归属校准版'):
        raise ValueError('校准稿被直接修改，请先将修改转为可重放记录')
    samples = read_json(paths['samples'])
    if samples.get('revision') != workflow.get('sample_revision') or samples.get('revision') != review.fingerprint([revision, names]):
        raise ValueError('采样与最终归属版本不一致')
    if not samples.get('groups') or any(g['status'] not in ('accepted', 'rejected', 'insufficient') for g in samples['groups']):
        raise ValueError('仍有待确认采样，请逐人完成准确或不对/不确定的选择')
    selected = paths['reviewed'] if paths['reviewed'].exists() else paths['separated']
    return {'raw': raw, 'paths': paths, 'meta_path': meta_path, 'meta': meta, 'rows': rows,
            'names': names, 'samples': samples, 'source': selected,
            'source_sha256': sha(selected), 'review_revision': revision}


def brief(ctx: dict) -> dict:
    """Expose only the final text, indices and version needed for authoring minutes."""
    return {'schema': 1, 'source_sha256': ctx['source_sha256'], 'review_revision': ctx['review_revision'],
            'duration': edits.fmt_ts(ctx['meta']['duration_ms']),
            'speakers': list(dict.fromkeys(ctx['names'].values())),
            'sentences': [{'index': r['_oi'], 'time': edits.fmt_ts(r['begin_time']),
                           'speaker': r.get('_manual_name') or ctx['names'][r['speaker_id']], 'text': r['text']} for r in ctx['rows']]}


def validate(content: dict, ctx: dict) -> None:
    """Reject stale or structurally ungrounded content; semantics are reviewed by the authoring agent."""
    if content.get('schema') != 1 or any(content.get(k) != ctx[k] for k in ('source_sha256', 'review_revision')):
        raise ValueError('纪要内容未绑定当前确认稿版本；请重新阅读最终稿并整理内容')
    def text(value, label):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f'{label} 需要非空文本')
    valid = {r['_oi'] for r in ctx['rows']}
    def grounded(item, field='text'):
        if not isinstance(item, dict):
            raise ValueError('纪要条目必须为对象')
        text(item.get(field), field)
        refs = item.get('sources')
        if not isinstance(refs, list) or not refs or any(type(i) is not int or i not in valid for i in refs):
            raise ValueError('每条纪要需引用存在的原始句索引')
    text(content.get('title'), 'title')
    for field in ('meeting_date', 'location', 'organizer', 'source_note'):
        if content.get(field) is not None:
            text(content[field], field)
    for key in ('summary', 'decisions', 'open_questions', 'agenda', 'tasks'):
        if not isinstance(content.get(key), list):
            raise ValueError(f'{key} 需要列表；没有明确内容时使用空列表')
    if not content['summary'] or not content['agenda']:
        raise ValueError('报告必须包含摘要和议程')
    for item in content['summary'] + content['decisions'] + content['open_questions']:
        grounded(item)
    for item in content['agenda']:
        text(item.get('title'), 'agenda.title')
        start, end = item.get('start'), item.get('end')
        if type(start) is not int or type(end) is not int or start not in valid or end not in valid or start > end:
            raise ValueError('议程起止需使用有效且有序的原始句索引')
        if not isinstance(item.get('points'), list) or not item['points']:
            raise ValueError('议程需要具体讨论要点')
        for point in item['points']:
            grounded(point)
            if any(not start <= i <= end for i in point['sources']):
                raise ValueError('议程引用超出该议程范围')
    for item in content['tasks']:
        grounded(item, 'task')
        if item.get('status') not in ('待办', '待确认'):
            raise ValueError('待办状态使用待办或待确认，不推断已经完成')
        for key in ('owner', 'due'):
            if item.get(key) is not None:
                text(item[key], key)


def local_href(path: Path, parent: Path) -> str:
    """Keep colocated deliverables portable; use a file URI only across Windows drives."""
    try:
        return quote(os.path.relpath(path.resolve(), parent.resolve()).replace('\\', '/'), safe='/')
    except ValueError:
        return path.resolve().as_uri()


def render(content: dict, ctx: dict, output: Path) -> str:
    """Render escaped text into an entirely local, responsive and printable template."""
    validate(content, ctx)
    e = lambda x: html.escape(str(x), quote=True)
    def refs(indices):
        return '<span class="refs">' + ''.join(f'<a href="#sentence-{i}" aria-label="查看句{i}原文">句{i}</a>' for i in dict.fromkeys(indices)) + '</span>'
    def items(values, empty):
        return '<ul class="points">' + ''.join(f'<li>{e(v["text"])} {refs(v["sources"])}</li>' for v in values) + '</ul>' if values else f'<p class="muted">{empty}</p>'
    rows = ctx['rows']
    by_index = {}
    for row in rows:
        by_index.setdefault(row['_oi'], []).append(row)
    agenda = []
    for n, item in enumerate(content['agenda'], 1):
        clock = edits.fmt_ts(by_index[item['start']][0]['begin_time']) + ' — ' + edits.fmt_ts(by_index[item['end']][-1]['end_time'])
        agenda.append(f'<article class="agenda-item"><div class="ordinal">{n:02d}</div><div><p class="eyebrow">{clock}</p><h3>{e(item["title"])}</h3>{items(item["points"], "")}</div></article>')
    tasks = []
    for n, task in enumerate(content['tasks'], 1):
        tasks.append(f'<tr data-status="{e(task["status"])}"><td class="task-number">{n:02d}</td><td><strong>{e(task["task"])}</strong>{refs(task["sources"])}</td><td>{e(task.get("owner") or "待确认")}</td><td>{e(task.get("due") or "未明确")}</td><td><span class="tag">{e(task["status"])}</span></td></tr>')
    links = []
    def link(label, path, detail='', publishing=False):
        if path.is_file() or publishing:
            links.append(f'<a class="file-card" href="{e(local_href(path, output.parent))}"><span>{e(label)}</span><small>{e(detail or path.name)}</small><b aria-hidden="true">↗</b></a>')
    for key, label in [('reviewed','归属校准版'),('separated','说话人分离稿'),('attributed','首次归属未确认版'),('original','原始转写稿'),('attribution','首次归属快照')]:
        link(label, ctx['paths'][key])
    link('原始 ASR 结果', ctx['raw']); link('校准与流程记录', ctx['meta_path'])
    if ctx.get('content_path'):
        link('纪要内容数据', ctx['content_path'])
        link('报告版本记录', ctx['paths']['report_meta'], publishing=True)
    link('云任务与清理记录', ctx['raw'].with_name(ctx['raw'].name[:-len('.asr.json')]+'.task.json'))
    if ctx['meta'].get('source'):
        link('原录音', Path(ctx['meta']['source']))
    if ctx['samples'].get('audio'):
        link('处理音频', Path(ctx['samples']['audio']))
    transcript = []
    for idx, pieces in by_index.items():
        lines = ''.join(f'<p><b>{e(r.get("_manual_name") or ctx["names"][r["speaker_id"]])}</b> {e(r["text"])}</p>' for r in pieces)
        transcript.append(f'<article id="sentence-{idx}" class="transcript-row"><span>{edits.fmt_ts(pieces[0]["begin_time"])} · 句{idx}</span><div>{lines}</div></article>')
    participants = '、'.join(dict.fromkeys((r.get('_manual_name') or ctx['names'][r['speaker_id']]) for r in rows))
    facts = [('会议日期',content.get('meeting_date') or '未明确'),('会议形式 / 地点',content.get('location') or '未明确'),('主持 / 组织',content.get('organizer') or '未明确'),('转写覆盖至',edits.fmt_ts(ctx['meta']['duration_ms'])),('已确认发言人',participants)]
    values = dict(title=e(content['title']), css=(ASSETS/'minutes.css').read_text(encoding='utf-8'),
                  facts=''.join(f'<div><dt>{e(k)}</dt><dd>{e(v)}</dd></div>' for k,v in facts),
                  summary=items(content['summary'],''),decisions=items(content['decisions'],'未提取到明确结论；不将讨论建议视为已决议。'),
                  questions=items(content['open_questions'],'未提取到明确待澄清事项。'),agenda=''.join(agenda),
                  tasks=''.join(tasks) or '<tr><td colspan="5">未提取到明确待办事项。</td></tr>',
                  task_count=len(tasks),agenda_count=len(agenda),speaker_count=len(set(ctx['names'].values())),
                  links=''.join(links),transcript=''.join(transcript),
                  source=e(ctx['source'].name),source_hash=ctx['source_sha256'][:16],
                  generated=e(datetime.now().astimezone().isoformat(timespec='minutes')),
                  source_note=e(content.get('source_note') or '根据最终归属稿整理，未对发言中的外部事实作独立核验。'))
    return Template((ASSETS/'minutes.html').read_text(encoding='utf-8')).substitute(values)


def generate(raw: Path, content_path: Path) -> Path:
    """Publish report and provenance while holding the same ASR lock as transcription edits."""
    with file_lock(raw):
        ctx = context(raw)
        content = read_json(content_path)
        ctx['content_path'] = content_path
        output = ctx['paths']['report']
        text = render(content, ctx, output)
        if content_path.resolve() in {output.resolve(),ctx['paths']['report_meta'].resolve()}:
            raise ValueError('纪要内容不能与报告输出路径相同')
        atomic_text(output, text)
        atomic_json(ctx['paths']['report_meta'], {'schema':1, 'source':ctx['source'].name,
                    'source_sha256':ctx['source_sha256'], 'review_revision':ctx['review_revision'],
                    'samples_sha256':sha(ctx['paths']['samples']), 'content_sha256':sha(content_path),
                    'html_sha256':sha(output), 'generated_utc':datetime.now(timezone.utc).isoformat()})
        return output


def main(argv=None) -> int:
    """Provide context for the agent, or render content it has grounded in that context."""
    parser = argparse.ArgumentParser(description='最终归属确认后生成离线单页 HTML 会议纪要')
    parser.add_argument('--from-json', required=True, type=Path)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--context', action='store_true', help='输出最终句索引与版本供智能体整理纪要')
    action.add_argument('--content', type=Path, help='按 references/report.md 整理的 .minutes.json')
    args = parser.parse_args(argv)
    try:
        if args.context:
            with file_lock(args.from_json):
                print(json.dumps(brief(context(args.from_json)),ensure_ascii=False,indent=2))
        else:
            print(f'[out] 会议纪要: {generate(args.from_json,args.content)}')
        return 0
    except (OSError,ValueError,KeyError,TypeError,RuntimeError) as exc:
        print(f'[report] {exc}')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
