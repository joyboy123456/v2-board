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
VERDICT = [('未通过', 'fail'), ('不通过', 'fail'), ('失败', 'fail'), ('部分', 'partial'), ('未完成', 'pending'),
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


def parse(board: Path, path: Path):
    raw = path.read_bytes()
    meta, body = front(raw.decode('utf-8', errors='replace'))
    parts = re.split(r'^##\s+', body, flags=re.M)
    intro, items = parts[0].strip(), []
    for chunk in parts[1:]:
        title, _, rest = chunk.partition('\n')
        cards, verdict, lines = [], '', []
        for line in rest.splitlines():
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
    if not d.is_dir():
        return out, errors
    for p in sorted(d.glob('*.md')):
        try:
            out.append(parse(board, p))
        except Exception as e:  # 一份坏报告不拖垮看板
            errors.append(f'验收报告 {p.name} 解析失败：{e}')
    out.sort(key=lambda r: r['date'], reverse=True)
    return out, errors


def safe_file(board: Path, rel: str):
    target = (board / rel).resolve()
    root = board.resolve()
    if root in target.parents and target.suffix.lower() in MEDIA_EXT and target.is_file():
        return target
    return None
