"""验收报告：只读解析 V2_BOARD_DIR/reports/*.md。

格式：
---
title: 主流程真实浏览器验收
date: 2026-11-17 14:55
by: Hark
site: https://oai.de5.net/v2
---
开头一段说明（可选）

## 每个检查项一个二级标题
卡：02、H1
结论：未通过            （通过 / 未通过 / 部分通过 / 未完成）
正文 markdown ……
![截图说明](../shots/acc-tfix/xxx.png)
"""
import hashlib
import re
from pathlib import Path

IMG_EXT = {'.png', '.jpg', '.jpeg', '.webp', '.gif'}
VID_EXT = {'.mp4', '.webm'}
MEDIA_EXT = IMG_EXT | VID_EXT
VERDICT = [('未通过', 'fail'), ('不通过', 'fail'), ('未能通过', 'fail'), ('无法通过', 'fail'), ('没有通过', 'fail'), ('没通过', 'fail'),
           ('失败', 'fail'), ('部分', 'partial'), ('未完成', 'pending'),
           ('待复测', 'pending'), ('通过', 'pass')]
IMG = re.compile(r'!\[([^\]]*)\]\(([^)\s]+)\)')


def verdict_of(text: str) -> str:
    return next((v for k, v in VERDICT if k in text), 'note')


def front(text: str):
    meta = {}
    if text.startswith('---'):
        end = text.find('\n---', 3)
        if end > 0:
            for line in text[3:end].splitlines():
                if ':' in line:
                    k, v = line.split(':', 1)
                    meta[k.strip()] = v.strip().strip('"\'')
            text = text[end + 4:]
    return meta, text.lstrip('\n')


def file_url(board: Path, base: Path, src: str) -> str:
    if re.match(r'^https?://', src):
        return src
    target = (base / src).resolve()
    root = board.resolve()
    if root not in target.parents or target.suffix.lower() not in MEDIA_EXT:
        return ''
    return '/api/file/' + str(target.relative_to(root))


def split_cards(v: str):
    return [x for x in re.split(r'[、,，\s]+', v) if x]


def split_items(body: str):
    """按二级标题分段；围栏代码块里的 `##` 不算标题。返回 [引言, 条目, ...]，条目不含开头的 `## `。"""
    parts, current, fence = [], [], ''
    for line in body.splitlines():
        mark = re.match(r'^\s*(`{3,}|~{3,})(.*)$', line)
        if mark:
            if not fence:
                fence = mark.group(1)
            elif mark.group(1)[0] == fence[0] and len(mark.group(1)) >= len(fence) and not mark.group(2).strip():
                fence = ''  # 收尾围栏要同一种符号、不短于开头，后面不带说明文字
        heading = None if fence else re.match(r'^##\s+(.*)$', line)
        if heading:
            parts.append('\n'.join(current))
            current = [heading.group(1)]
        else:
            current.append(line)
    parts.append('\n'.join(current))
    return parts


def parse(board: Path, path: Path):
    raw = path.read_bytes()
    meta, body = front(raw.decode('utf-8', errors='replace'))
    parts = split_items(body)
    intro, items = parts[0].strip(), []
    for chunk in parts[1:]:
        title, _, rest = chunk.partition('\n')
        cards, verdict, lines = [], '', []
        for line in rest.splitlines():
            if not lines and not line.strip():
                continue  # 标题和「卡：/结论：」之间允许空行
            m = re.match(r'^\s*(卡|结论)[:：]\s*(.+)$', line)
            if m and not lines:
                if m.group(1) == '卡':
                    cards = split_cards(m.group(2))
                else:
                    verdict = m.group(2).strip()
                continue
            lines.append(line)
        text = '\n'.join(lines).strip()
        shots = []
        for alt, src in IMG.findall(text):
            url = file_url(board, path.parent, src)
            kind = 'video' if re.search(r'\.(mp4|webm)(\?|$)', src, re.I) else 'image'
            shots.append(dict(alt=alt, src=url, kind=kind, missing=not url or (not url.startswith('http') and not (board / url[len('/api/file/'):]).exists())))
        text = IMG.sub('', text).strip()
        items.append(dict(title=title.strip(), cards=cards, verdict=verdict_of(verdict) if verdict else 'note',
                          verdictText=verdict, text=text, shots=shots))
    counts = {}
    for it in items:
        counts[it['verdict']] = counts.get(it['verdict'], 0) + 1
    return dict(id=path.stem, revision=hashlib.sha256(raw).hexdigest(), title=meta.get('title', path.stem), date=meta.get('date', ''), by=meta.get('by', ''),
                site=meta.get('site', ''), shotsNote=meta.get('shots', ''), intro=intro, items=items, counts=counts,
                cards=sorted({c for it in items for c in it['cards']}))


def collect(board: Path):
    d = board / 'reports'
    out, errors = [], []
    if d.is_symlink() or not d.is_dir():
        return out, errors
    for p in sorted(d.glob('*.md')):
        if p.is_symlink() or not p.is_file():
            continue  # 不跟随符号链接，免得把看板外的文件内容带进响应
        try:
            out.append(parse(board, p))
        except Exception as e:  # 一份坏报告不拖垮看板
            errors.append(f'验收报告 {p.name} 解析失败：{e}')
    out.sort(key=lambda r: r['date'], reverse=True)
    return out, errors


def evidence(board: Path):
    """验收报告里引用到的本地截图、录屏（解析后的绝对路径）。"""
    root, found = board.resolve(), set()
    for report in collect(board)[0]:
        for item in report['items']:
            for shot in item['shots']:
                if shot['src'].startswith('/api/file/'):
                    found.add((root / shot['src'][len('/api/file/'):]).resolve())
    return found


def safe_file(board: Path, rel: str):
    """只放行看板目录内、被某份验收报告引用的图片或视频。"""
    target = (board / rel).resolve()
    root = board.resolve()
    if root in target.parents and target.suffix.lower() in MEDIA_EXT and target.is_file() and target in evidence(board):
        return target
    return None
