import { useCallback, useEffect, useState, type FormEvent } from 'react';
import { Layers } from 'lucide-react';
import App from './App';
import { auth, LOGIN_EVENT } from './lib';

type Phase = 'checking' | 'login' | 'in' | 'offline';

/* 看板的门：开了登录的实例先登录，之后 30 天靠 cookie；没开登录的实例直接进。 */
export default function Gate() {
  const [phase, setPhase] = useState<Phase>('checking');
  const check = useCallback(async () => {
    try {
      const response = await fetch('/api/session', { cache: 'no-store' });
      if (!response.ok) throw new Error('session');
      const state = await response.json() as { login: boolean; authed: boolean };
      auth.session = state.login && state.authed;
      setPhase(state.login && !state.authed ? 'login' : 'in');
    } catch {
      setPhase(current => current === 'in' ? current : 'offline');
    }
  }, []);
  useEffect(() => {
    void check();
    const out = () => { auth.session = false; setPhase('login'); };
    window.addEventListener(LOGIN_EVENT, out);
    return () => window.removeEventListener(LOGIN_EVENT, out);
  }, [check]);
  if (phase === 'in') return <App />;
  if (phase === 'login') return <Login done={check} />;
  return <main className="login"><div className="login-card">
    <p className="hint">{phase === 'offline' ? '连不上看板服务。' : '正在打开看板…'}</p>
    {phase === 'offline' && <button className="login-submit" onClick={() => { setPhase('checking'); void check(); }}>重试</button>}
  </div></main>;
}

function Login({ done }: { done: () => Promise<void> }) {
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!password || busy) return;
    setBusy(true); setError('');
    try {
      const response = await fetch('/api/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password }) });
      if (response.ok) { await done(); return; }
      const data = await response.json().catch(() => ({})) as { detail?: string };
      setError(data.detail || '登录失败 (' + response.status + ')');
    } catch {
      setError('连不上看板服务，稍后再试。');
    } finally {
      setBusy(false);
    }
  };
  return <main className="login">
    <form className="login-card" onSubmit={submit}>
      <div className="logo"><Layers size={18} strokeWidth={2} /></div>
      <h1>V2 开发看板</h1>
      <p className="hint">登录一次，这台设备 30 天内不用再输；之后指派修复、回答提问、发消息、停止都不用再填令牌。</p>
      {/* 给浏览器的密码管理器一个账号名，方便它记住密码 */}
      <input type="text" name="username" autoComplete="username" value="v2-board" readOnly hidden />
      <label>密码<input type="password" name="password" autoComplete="current-password" autoFocus value={password} onChange={e => { setPassword(e.target.value); setError(''); }} /></label>
      {error && <p className="login-error" role="alert">{error}</p>}
      <button className="login-submit" disabled={!password || busy}>{busy ? '登录中…' : '登录'}</button>
    </form>
  </main>;
}
