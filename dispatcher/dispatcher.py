#!/usr/bin/env python3
"""v2-dispatch: a resident dispatcher that keeps server agents working.

    dispatcher.py run       loop forever (systemd)
    dispatcher.py once      one pass, then exit
    dispatcher.py status    JSON: what needs a person, what runs, what waits

A card joins the queue when its front matter has `dispatch: queued` and an
`executor`. See dispatcher/README.md for the card fields and the ask protocol.
Standard library only.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cards  # noqa: E402

DEFAULTS = {
    'board': '/srv/v2-wiring-board',
    'state': '/var/lib/v2-dispatch',
    'slots': 3,
    'min_free_mem_gb': 4,
    'poll_seconds': 5,
    'run_timeout_seconds': 21600,
    'stop_grace_seconds': 10,
    'agent_run': 'agent-run',
    'link': ['node_modules'],
    'worktree_root': '/root/wt',
    'executors': {},
}

SESSION_PATTERNS = (
    re.compile(r'"type"\s*:\s*"thread\.started".*?"thread_id"\s*:\s*"([^"]+)"'),
    re.compile(r'"session_?[iI]d"\s*:\s*"([^"]+)"'),
)

PROTOCOL = """

---
## 调度协议（v2-dispatch 自动附加）
- 卡号 {card}。你在 worktree `{worktree}` 的分支 `{branch}` 上工作，只改这里，不动其他目录。
- 需要人拍板时（方案取舍、缺凭据、范围不清），不要猜：执行
  `board-ask {card} "<一个具体问题>" --option "<选项一>" --option "<选项二>"`
  然后立刻结束本轮。回答到了以后，你会在同一个会话里被唤醒继续。
- 中途进度：`board-report {card} --progress "<一句话>"`。
- 完成：提交到本分支（中文提交信息），不推送、不部署、不改其他分支。最后一条消息写清改了什么、测试结果、还有什么没做。
"""

RESUME = """{said}

请在同一任务里继续，协议同前（卡号 {card}，worktree `{worktree}`，分支 `{branch}`）。"""


def log(*parts):
    print(time.strftime('%Y-%m-%d %H:%M:%S'), *parts, flush=True)


class Dispatcher:
    def __init__(self, config: dict):
        self.cfg = {**DEFAULTS, **config}
        self.board = Path(self.cfg['board'])
        self.state = Path(self.cfg['state'])
        self.state.mkdir(parents=True, exist_ok=True)
        self.children: dict[str, subprocess.Popen] = {}

    # ---- persistent run records -------------------------------------------
    def rec_path(self, card):
        return self.state / f'{card}.json'

    def rec(self, card):
        try:
            return json.loads(self.rec_path(card).read_text())
        except (OSError, ValueError):
            return {}

    def save(self, card, **changes):
        data = {**self.rec(card), **changes}
        tmp = self.rec_path(card).with_suffix('.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        os.replace(tmp, self.rec_path(card))
        return data

    def event(self, kind, card, **fields):
        line = json.dumps(dict(at=cards.stamp(), kind=kind, card=card, **fields), ensure_ascii=False)
        with open(self.state / 'events.jsonl', 'a', encoding='utf-8') as f:
            f.write(line + '\n')
        log(kind, card, fields.get('summary', ''))

    # ---- helpers --------------------------------------------------------------
    @staticmethod
    def alive(pid):
        if not pid:
            return False
        try:
            os.kill(int(pid), 0)
        except (ProcessLookupError, ValueError):
            return False
        except PermissionError:
            return True
        try:  # a zombie is not alive
            return Path(f'/proc/{pid}/stat').read_text().split()[2] != 'Z'
        except OSError:
            return True

    @staticmethod
    def free_mem_gb():
        try:
            for line in Path('/proc/meminfo').read_text().splitlines():
                if line.startswith('MemAvailable:'):
                    return int(line.split()[1]) / 1048576
        except OSError:
            pass
        return 1e9

    @staticmethod
    def tree(pid):
        """pid and every live descendant, whichever session or process group they moved to."""
        kids = {}
        for stat in Path('/proc').glob('[0-9]*/stat'):
            try:
                kids.setdefault(int(stat.read_text().rsplit(')', 1)[1].split()[1]), []).append(int(stat.parent.name))
            except (OSError, ValueError, IndexError):
                continue
        out, todo = [], [int(pid)]
        while todo:
            out.append(todo.pop())
            todo.extend(kids.get(out[-1], []))
        return out

    @staticmethod
    def ident(pid):
        """A live process's start time: together with the pid it names one process, so a recycled pid is never mistaken for it."""
        try:
            fields = Path(f'/proc/{int(pid)}/stat').read_text().rsplit(')', 1)[1].split()
        except (OSError, ValueError, IndexError, TypeError):
            return None
        return None if fields[0] == 'Z' else fields[19]

    def halt(self, card):
        """Someone pressed 停止 on the board. TERM first, so agent-run can pass it on and record the run; KILL what is left after the grace period.

        The whole process tree is written down before the first signal: a child the agent detached (setsid) is no longer
        findable once its parent exits, so sweep() finishes those off from the list after the turn has ended.
        """
        rec = self.rec(card)
        child = self.children.get(card)
        pid = child.pid if child else rec.get('pid')
        if not pid:
            return
        if not child and (not rec.get('pid_start') or self.ident(pid) != rec['pid_start']):
            self.finish(card, None)  # 重启后接手的记录：这个 pid 已经不是当初那个进程了，不发信号
            return
        sent = rec.get('stop_sent')
        if sent and time.time() - sent < float(self.cfg['stop_grace_seconds']):
            return
        known = {int(p): start for p, start in rec.get('stop_pids') or []}
        for p in self.tree(pid):
            known.setdefault(p, self.ident(p))
        tracked = [[p, start] for p, start in known.items() if start]
        sig = signal.SIGKILL if sent else signal.SIGTERM
        self.save(card, stop_sent=sent or time.time(), stop_pids=tracked)
        try:  # launched with start_new_session, so the pid leads its own process group
            if os.getpgid(int(pid)) == int(pid):
                os.killpg(int(pid), sig)
        except (ProcessLookupError, PermissionError):
            pass
        for p, start in tracked if sent else [[int(pid), known.get(int(pid))]]:
            self.signal(p, start, sig)
        if not sent:
            self.event('stopping', card, summary='有人在看板上点了停止，正在结束这一轮')

    def signal(self, pid, start, sig):
        if start and self.ident(pid) == start:
            try:
                os.kill(int(pid), sig)
            except (ProcessLookupError, PermissionError):
                pass

    def sweep(self, busy):
        """After a stopped turn has ended: KILL whatever it left behind once the grace period is over. Returns the cards still waiting on that."""
        waiting = set()
        for path in self.state.glob('*.json'):
            card = path.stem
            rec = self.rec(card)
            if card in busy or not rec.get('stop_pids'):
                continue
            left = [[p, start] for p, start in rec['stop_pids'] if self.ident(p) == start]
            if not left:
                self.save(card, stop_pids=None, stop_sent=None)
                continue
            waiting.add(card)
            if time.time() - (rec.get('stop_sent') or 0) >= float(self.cfg['stop_grace_seconds']):
                for p, start in left:
                    self.signal(p, start, signal.SIGKILL)
        return waiting

    def shelve(self, path):
        """A stop request for a card that is not running: one still waiting for a slot is held, anything else just loses the request."""
        held = False
        with cards.edit(path) as box:
            meta, _ = cards.parse(box[0])
            if not meta.get('stop'):
                return
            text = cards.drop_keys(box[0], 'stop')
            if meta.get('dispatch') in ('queued', 'answered'):
                text, held = cards.set_keys(text, dispatch='held', stopped=cards.stamp()), True
            box[0] = text
        if held:
            self.event('stopped', path.stem, summary='还没开始这一轮就被人在看板上停止了')

    def running(self):
        return [c for c in self.children] + [
            p.stem for p in self.state.glob('*.json')
            if p.stem not in self.children and self.alive(self.rec(p.stem).get('pid'))]

    def worktree(self, card, meta):
        repo = meta.get('repo')
        wt = meta.get('worktree') or str(Path(self.cfg['worktree_root'], card.lower()))
        if not repo:
            wt = Path(meta.get('worktree') or self.state / 'work' / card)
            wt.mkdir(parents=True, exist_ok=True)
            return str(wt)
        if Path(wt).exists():
            return wt
        branch = meta.get('branch') or f'd/{card.lower()}'
        base = meta.get('base') or 'HEAD'
        Path(wt).parent.mkdir(parents=True, exist_ok=True)
        has = subprocess.run(['git', '-C', repo, 'rev-parse', '--verify', '--quiet', f'refs/heads/{branch}'], capture_output=True).returncode == 0
        args = ['git', '-C', repo, 'worktree', 'add', wt, branch] if has else ['git', '-C', repo, 'worktree', 'add', '-b', branch, wt, base]
        p = subprocess.run(args, capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError(f'worktree 创建失败：{p.stderr.strip()[:300]}')
        for name in self.cfg['link']:
            src, dst = Path(repo, name), Path(wt, name)
            if src.exists() and not dst.exists():
                dst.symlink_to(src)
        return wt

    @staticmethod
    def task_text(body):
        for head in (cards.Q_HEAD, cards.A_HEAD, cards.M_HEAD):
            body = re.sub(rf'^{re.escape(head)}\s*$.*?(?=^## |\Z)', '', body, flags=re.M | re.S)
        return body.strip()

    def command(self, spec, key, **values):
        out = []
        for part in spec[key]:
            out.append(part.format(**values) if isinstance(part, str) else str(part))
        return out

    # ---- launching --------------------------------------------------------------
    def launch(self, path, meta, body, resume=False):
        card = path.stem
        spec = self.cfg['executors'].get(meta.get('executor', ''))
        if not spec:
            cards.update(path, dispatch='failed')
            self.event('failed', card, summary=f"未知执行器 {meta.get('executor')!r}")
            return
        rec = self.rec(card)
        try:
            wt = rec.get('worktree') if resume and rec.get('worktree') else self.worktree(card, meta)
        except RuntimeError as error:
            cards.update(path, dispatch='failed')
            self.event('failed', card, summary=str(error))
            return
        branch = meta.get('branch') or subprocess.run(['git', '-C', wt, 'branch', '--show-current'], capture_output=True, text=True).stdout.strip()
        attempt = int(rec.get('attempt', 0)) + 1
        run_dir = self.state / 'runs' / card
        run_dir.mkdir(parents=True, exist_ok=True)
        final = run_dir / f'{attempt}-final.md'
        out = run_dir / f'{attempt}.log'
        ctx = dict(card=card, worktree=wt, branch=branch)
        answered = cards.answers(body)
        total = len(cards.messages(body))
        # 有的 CLI 续聊不用会话号（在同一个 worktree 里「接着上一次」），resume 命令里没有 {session} 就不要求
        needs_session = any('{session}' in part for part in spec.get('resume', []) if isinstance(part, str))
        if resume and 'resume' in spec and (rec.get('session') or (not needs_session and rec.get('attempt'))):  # 从没跑过的卡没有可续的会话，按第一轮给完整任务
            said = self.said_text(body, skip=rec.get('answers_sent') or (), pending=cards.pending_messages(meta, body))
            prompt = RESUME.format(said=said or self.said_text(body), **ctx)
            key = 'resume'
        else:
            # 第一轮，或续不上原会话：从头给任务，带上至今所有回答和留言
            prompt = self.task_text(body) + PROTOCOL.format(**ctx)
            said = self.said_text(body, pending=cards.messages(body)) if resume else self.said_text(body, answers=False, pending=cards.pending_messages(meta, body))
            if said:
                prompt += '\n\n' + said
            key = 'cmd'
        cmd = self.command(spec, key, prompt=prompt, final=str(final), session=rec.get('session', ''), worktree=wt, card=card)
        timeout = int(meta.get('timeout') or spec.get('timeout') or self.cfg['run_timeout_seconds'])
        if self.cfg.get('agent_run'):
            # agent-run recognises codex/claude by cmd[0], so the timeout wraps it from outside;
            # on timeout agent-run forwards the signal to the agent and reports the run.
            cmd = [self.cfg['agent_run'], card, spec.get('agent', meta['executor']), wt, f'FINAL={final}', '--', *cmd]
        cmd = ['timeout', str(timeout), *cmd]
        env = dict(os.environ, **{k: str(v) for k, v in spec.get('env', {}).items()}, V2_BOARD_DIR=str(self.board), BOARD_ROOT=str(self.board))
        sink = open(out, 'ab')
        child = subprocess.Popen(cmd, cwd=wt, env=env, stdin=subprocess.DEVNULL, stdout=sink, stderr=subprocess.STDOUT, start_new_session=True)
        sink.close()
        self.children[card] = child
        self.save(card, pid=child.pid, pid_start=self.ident(child.pid), attempt=attempt, worktree=wt, branch=branch, executor=meta['executor'], log=str(out), final=str(final), started=cards.stamp(), mode=key, answers_sent=sorted(answered))
        # delivered 记的是这一轮提示词里带上的留言数；launch 之后才到的留言留给下一轮
        with cards.edit(path) as box:  # stopped 是上一次被人停止留下的标记，重新开跑就清掉
            box[0] = cards.drop_keys(cards.set_keys(box[0], dispatch='running', delivered=total), 'stopped')
        self.event('resumed' if resume else 'started', card, executor=meta['executor'], attempt=attempt, summary=f"{spec.get('agent', meta['executor'])} 第 {attempt} 轮")

    @staticmethod
    def answers_text(body, skip=()):
        q, a = cards.questions(body), cards.answers(body)
        lines = []
        for qid, text in q.items():
            if qid in a and qid not in skip:
                lines.append(f'- {qid} 问：{text}\n  答：{a[qid]}')
        return '\n'.join(lines)

    @classmethod
    def said_text(cls, body, skip=(), pending=None, answers=True):
        """What people told the agent since its last turn: new answers, then messages from the board chat."""
        parts = []
        answered = cls.answers_text(body, skip) if answers else ''
        if answered:
            parts.append('有人回答了你的问题：\n' + answered)
        if pending:
            parts.append('用户在看板的对话窗口里对你说：\n' + '\n'.join(f'- {cards.message_text(text)}' for mid, text in pending))
        return '\n\n'.join(parts)

    # ---- finishing --------------------------------------------------------------
    def session_from(self, logfile):
        try:
            with open(logfile, 'r', encoding='utf-8', errors='replace') as f:
                head = f.read(200000)
        except OSError:
            return ''
        for pat in SESSION_PATTERNS:
            m = pat.search(head)
            if m:
                return m.group(1)
        return ''

    def finish(self, card, rc):
        rec = self.rec(card)
        session = self.session_from(rec.get('log', '')) or rec.get('session', '')
        path = cards.find(self.board, card)
        new, halted = 'done' if rc == 0 else 'failed', False
        if path:
            # 判断和改写在同一把锁里做，这一刻刚写进来的留言或回答不会被漏掉
            with cards.edit(path) as box:
                meta, body = cards.parse(box[0])
                state = meta.get('dispatch')
                if state == 'asking' or cards.open_questions(body):
                    new = 'asking'
                elif state == 'answered' or cards.pending_messages(meta, body):
                    new = 'answered'  # answered or messaged while the worker was still running: resume next pass
                elif rc == 0 or (rc is None and meta.get('status', '') in ('review', 'accepted')):
                    new = 'done'
                else:
                    new = 'failed'
                text = box[0]
                if meta.get('stop'):
                    # 有人点了停止：这一轮不算失败，会话留着；它自己正常跑完的话仍按完成算
                    text = cards.drop_keys(text, 'stop')
                    if new in ('failed', 'answered'):
                        new, halted = 'held', True
                        text = cards.set_keys(text, stopped=cards.stamp())
                if state != new:
                    text = cards.set_keys(text, dispatch=new)
                box[0] = text
        summary = ''
        try:
            summary = Path(rec.get('final', '')).read_text(errors='replace').strip()[:300]
        except OSError:
            pass
        self.save(card, pid=None, session=session, exit_code=rc, ended=cards.stamp())
        self.event('stopped' if halted else new, card, exit_code=rc, summary='被人在看板上停止，会话和 worktree 保留' if halted else summary or f'退出码 {rc}')

    def reap(self):
        for card, child in list(self.children.items()):
            rc = child.poll()
            if rc is not None:
                del self.children[card]
                self.finish(card, rc)
        # runs adopted from an earlier dispatcher process
        for p in self.state.glob('*.json'):
            card = p.stem
            pid = self.rec(card).get('pid')
            if card not in self.children and pid and not self.alive(pid):
                self.finish(card, None)

    # ---- one pass ---------------------------------------------------------------
    def tick(self):
        self.reap()
        busy = set(self.running())
        busy |= self.sweep(busy)  # 被停止的那一轮还有残留进程没清完：先不让这张卡再开跑
        todo = []
        for path, meta, body in cards.scan(self.board):
            card, state = path.stem, meta.get('dispatch')
            if meta.get('stop'):
                if card in self.children or self.alive(self.rec(card).get('pid')):
                    self.halt(card)
                elif state == 'running':
                    self.finish(card, None)
                else:
                    self.shelve(path)
                continue
            if card in busy:
                continue
            if state == 'running':
                # running on the card but no live process: the previous dispatcher died mid-launch
                self.finish(card, None)
            elif state in ('queued', 'answered'):
                todo.append((int(meta.get('priority') or 50), path.stat().st_mtime, path, meta, body, state == 'answered'))
        todo.sort(key=lambda t: (t[0], t[1]))
        for _, _, path, meta, body, resume in todo:
            if len(busy) >= int(self.cfg['slots']):
                break
            if self.free_mem_gb() < float(self.cfg['min_free_mem_gb']):
                log('内存不足，暂缓派发')
                break
            self.launch(path, meta, body, resume=resume)
            busy.add(path.stem)

    def run(self):
        stop = []
        signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
        signal.signal(signal.SIGINT, lambda *_: stop.append(1))
        log('v2-dispatch 启动', f"槽位 {self.cfg['slots']}", f"看板 {self.board}")
        while not stop:
            try:
                self.tick()
            except Exception as error:  # keep the resident loop alive
                log('pass error', type(error).__name__, error)
            time.sleep(float(self.cfg['poll_seconds']))
        # Children run in their own sessions and keep working; the next start adopts them.
        log('v2-dispatch 退出，进行中的任务保留，下次启动接管')

    # ---- status for people and for Hark -----------------------------------------
    def status(self):
        groups = {k: [] for k in ('asking', 'failed', 'done', 'running', 'answered', 'queued', 'held', 'closed')}
        for path, meta, body in cards.scan(self.board):
            card = path.stem
            rec = self.rec(card)
            item = dict(card=card, title=meta.get('title', ''), executor=meta.get('executor', ''), status=meta.get('status', ''), attempt=rec.get('attempt', 0), started=rec.get('started'), ended=rec.get('ended'), branch=rec.get('branch') or meta.get('branch'))
            state = meta.get('dispatch')
            if state == 'asking':
                item['questions'] = cards.open_questions(body)
            if state in ('failed', 'done'):
                try:
                    item['final'] = Path(rec.get('final', '')).read_text(errors='replace').strip()[:600]
                except OSError:
                    item['final'] = ''
            groups[state].append(item)
        groups.pop('closed')
        return dict(at=cards.stamp(), slots=int(self.cfg['slots']), free_mem_gb=round(self.free_mem_gb(), 1),
                    needs_person=len(groups['asking']) + len(groups['failed']) + len(groups['done']), **groups)


def load_config():
    path = os.environ.get('DISPATCH_CONFIG', '/etc/v2-dispatch.json')
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {}


def main(argv):
    cmd = argv[1] if len(argv) > 1 else 'status'
    d = Dispatcher(load_config())
    if cmd == 'run':
        d.run()
    elif cmd == 'once':
        d.tick()
    elif cmd == 'status':
        print(json.dumps(d.status(), ensure_ascii=False, indent=1))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
