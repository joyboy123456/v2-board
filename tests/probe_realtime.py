#!/usr/bin/env python3
"""No browser. Exercise the actual SSE/REST protocol and real wrapper in isolation."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

REPO = Path(__file__).resolve().parents[1]


def read_json(base, path):
    with urllib.request.urlopen(base + path, timeout=4) as response:
        assert response.status == 200
        return json.load(response)


def main():
    with tempfile.TemporaryDirectory(prefix='board-realtime-') as folder:
        board = Path(folder)
        (board / '01.md').write_text('---\nid: "01"\ntitle: "实时信号验收"\nstatus: "assigned"\nassignee: "Codex gpt-6.1-sol"\n---\n')
        fake = board / 'codex'
        fake.write_text('''#!/usr/bin/env python3
import json,time
for i in range(5):
 print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':f'实时消息 {i}'}}),flush=True)
 time.sleep(.7)
''')
        fake.chmod(0o755)
        env = dict(os.environ, V2_BOARD_DIR=str(board), BOARD_ROOT=str(board), V2_FETCH_ENABLED='0')
        log = (board / 'server.log').open('w')
        server = subprocess.Popen(['nice', '-n', '19', str(REPO / '.venv/bin/uvicorn'), 'app:app', '--app-dir', str(REPO / 'backend'), '--host', '127.0.0.1', '--port', '3161'], stdout=log, stderr=log, env=env)
        agent = None
        stream = None
        base = 'http://127.0.0.1:3161'
        received = queue.Queue()
        try:
            for _ in range(100):
                try:
                    read_json(base, '/api/board')
                    break
                except OSError:
                    time.sleep(.05)
            else:
                raise AssertionError('preview did not start')
            stream = urllib.request.urlopen(base + '/api/stream', timeout=6)
            def reader():
                kind = ''
                try:
                    for raw in stream:
                        line = raw.decode().strip()
                        if line.startswith('event: '):
                            kind = line[7:]
                        if line.startswith('data: '):
                            received.put((time.monotonic(), kind, json.loads(line[6:])))
                except Exception:
                    pass
            thread = threading.Thread(target=reader, daemon=True)
            thread.start()
            started = time.monotonic()
            agent = subprocess.Popen(['nice', '-n', '19', str(REPO / 'scripts/agent-run'), '01', 'Codex gpt-6.1-sol', str(REPO), '--', str(fake), 'exec', 'fake'], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            live = []
            deadline = started + 2
            while time.monotonic() < deadline:
                stamp, kind, payload = received.get(timeout=max(.01, deadline - time.monotonic()))
                if kind == 'run:message':
                    live.append(payload)
                    latency = stamp - started
                    break
            assert live, 'SSE message missing within 2 seconds'
            run_id = live[0]['run_id']
            # Drop the transport, allow messages to accumulate, then replay after acknowledged seq.
            stream.close()
            stream = None
            agent.wait(timeout=10)
            after = max(m['seq'] for m in live)
            replay = read_json(base, f'/api/runs/{run_id}/messages?after_seq={after}')
            complete = read_json(base, f'/api/runs/{run_id}/messages')
            merged = {m['seq']: m for m in live}
            merged.update({m['seq']: m for m in replay})
            assert sorted(merged) == list(range(1, len(complete) + 1))
            assert len(merged) == len(complete)
            record = read_json(base, f'/api/runs/{run_id}')
            assert record['state'] == 'completed'
            assert record['exit_code'] == 0
            assert len(complete) == 5, complete
            with urllib.request.urlopen(base + '/api/stream', timeout=3) as resumed:
                assert resumed.status == 200
            for path in ('/api/agents', '/api/runs?agent=codex-gpt-6.1-sol&card=01&state=completed', '/api/events?since=0', '/api/board'):
                assert read_json(base, path)
            with urllib.request.urlopen(base + '/', timeout=3) as page:
                assert b'type="module"' in page.read()
            assert (board / f'runs/{run_id}.json').exists()
            assert [e['state'] for e in read_json(base, '/api/events') if e['type'] == 'run:status'] == ['running', 'completed']
            print(json.dumps(dict(first_message_seconds=round(latency, 3), messages=len(complete), replay=len(replay), duplicate_sequences=0, gaps=0, state=record['state'], api='all 200', page='200'), ensure_ascii=False))
        finally:
            if stream:
                stream.close()
            if agent and agent.poll() is None:
                agent.terminate()
                agent.wait(timeout=5)
            server.terminate()
            server.wait(timeout=10)
            log.close()


if __name__ == '__main__':
    main()
