import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { ArrowLeft, Plus, RefreshCw } from 'lucide-react';
import { apiFetch } from '../apiBase';
import '../styles/game.css';

const EMPTY_JSON = '{}';

function parseShanghaiDateTime(value) {
  if (!value) return null;
  const s = String(value).trim();
  if (!s) return null;
  if (/(Z|[+-]\d{2}:?\d{2})$/i.test(s)) return new Date(s);
  return new Date(`${s.replace(' ', 'T')}+08:00`);
}

function formatTime(value) {
  const d = parseShanghaiDateTime(value);
  if (!d || Number.isNaN(d.getTime())) return '—';
  return d.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' });
}

function jsonText(value) {
  return JSON.stringify(value ?? {}, null, 2);
}

async function readResponse(res) {
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.success === false) {
    throw new Error(data.detail || data.message || data.error || `HTTP ${res.status}`);
  }
  return data.data;
}

function JsonBlock({ value, empty = '—' }) {
  const hasValue = value && (Array.isArray(value) ? value.length : Object.keys(value).length);
  if (!hasValue) return <div className="game-empty-inline">{empty}</div>;
  return (
    <details className="game-json" open>
      <summary>JSON</summary>
      <pre>{jsonText(value)}</pre>
    </details>
  );
}

function ConfirmDialog({ title, message, danger, busy, onCancel, onConfirm }) {
  return (
    <div className="modal-overlay" role="presentation" onClick={() => !busy && onCancel()}>
      <div className="modal-container confirm-modal game-modal" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}>
        <div className="modal-title">{title}</div>
        <div className="confirm-message">{message}</div>
        <div className="confirm-warning">此操作会立即写入数据库。</div>
        <div className="modal-actions">
          <button type="button" className="modal-button cancel" onClick={onCancel} disabled={busy}>取消</button>
          <button type="button" className={`modal-button ${danger ? 'delete' : 'confirm'}`} onClick={onConfirm} disabled={busy}>
            {busy ? '处理中…' : '确认'}
          </button>
        </div>
      </div>
    </div>
  );
}

function SessionModal({ initial, onClose, onSaved }) {
  const isEdit = Boolean(initial?.id);
  const [form, setForm] = useState({
    game_type: initial?.game_type || '',
    display_name: initial?.display_name || '',
    system_prompt: initial?.system_prompt || '',
    config_json: jsonText(initial?.config_json || {}),
    state_json: jsonText(initial?.state_json || {}),
    participants: Array.isArray(initial?.participants) ? initial.participants.join(', ') : '',
    state_mode: initial?.state_mode || 'on_end',
  });
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const set = (key, value) => setForm((prev) => ({ ...prev, [key]: value }));

  const handleSave = async () => {
    setError('');
    let configJson;
    let stateJson;
    try {
      configJson = JSON.parse(form.config_json.trim() || EMPTY_JSON);
      stateJson = JSON.parse(form.state_json.trim() || EMPTY_JSON);
    } catch (e) {
      setError(`JSON 格式错误：${e.message}`);
      return;
    }
    if (!form.display_name.trim() || !form.system_prompt.trim() || !form.state_mode) {
      setError('display_name、system_prompt、state_mode 必填');
      return;
    }
    if (!isEdit && !form.game_type.trim()) {
      setError('game_type 必填');
      return;
    }
    const payload = {
      display_name: form.display_name.trim(),
      system_prompt: form.system_prompt,
      config_json: configJson,
      state_json: stateJson,
      participants: form.participants.split(',').map((v) => v.trim()).filter(Boolean),
      state_mode: form.state_mode,
    };
    if (!isEdit) payload.game_type = form.game_type.trim();
    setBusy(true);
    try {
      const path = isEdit ? `/api/game/sessions/${encodeURIComponent(initial.id)}` : '/api/game/sessions';
      await readResponse(await apiFetch(path, {
        method: isEdit ? 'PUT' : 'POST',
        body: JSON.stringify(payload),
      }));
      onSaved();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-overlay" role="presentation" onClick={() => !busy && onClose()}>
      <div className="modal-container game-modal game-session-modal" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}>
        <div className="modal-title">{isEdit ? '编辑游戏' : '新建游戏'}</div>
        {!isEdit && (
          <label className="game-field">
            <span>game_type</span>
            <input className="game-input" value={form.game_type} onChange={(e) => set('game_type', e.target.value)} />
          </label>
        )}
        <label className="game-field">
          <span>display_name</span>
          <input className="game-input" value={form.display_name} onChange={(e) => set('display_name', e.target.value)} />
        </label>
        <label className="game-field">
          <span>participants</span>
          <input className="game-input" value={form.participants} onChange={(e) => set('participants', e.target.value)} placeholder="南杉, Clio, Sirius" />
        </label>
        <label className="game-field">
          <span>state_mode</span>
          <select className="game-input" value={form.state_mode} onChange={(e) => set('state_mode', e.target.value)}>
            <option value="on_end">on_end</option>
            <option value="per_turn">per_turn</option>
          </select>
        </label>
        <label className="game-field">
          <span>system_prompt</span>
          <textarea className="game-textarea tall" value={form.system_prompt} onChange={(e) => set('system_prompt', e.target.value)} />
        </label>
        <label className="game-field">
          <span>config_json</span>
          <textarea className="game-textarea" value={form.config_json} onChange={(e) => set('config_json', e.target.value)} />
        </label>
        <label className="game-field">
          <span>state_json</span>
          <textarea className="game-textarea" value={form.state_json} onChange={(e) => set('state_json', e.target.value)} />
        </label>
        {error && <div className="game-form-error">{error}</div>}
        <div className="modal-actions">
          <button type="button" className="modal-button cancel" onClick={onClose} disabled={busy}>取消</button>
          <button type="button" className="modal-button confirm" onClick={handleSave} disabled={busy}>{busy ? '保存中…' : '保存'}</button>
        </div>
      </div>
    </div>
  );
}

function TurnModal({ initial, sessionId, onClose, onSaved }) {
  const [text, setText] = useState(jsonText(initial?.turn_data || {}));
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const isEdit = Boolean(initial?.id);

  const handleSave = async () => {
    setError('');
    let turnData;
    try {
      turnData = JSON.parse(text.trim() || EMPTY_JSON);
    } catch (e) {
      setError(`JSON 格式错误：${e.message}`);
      return;
    }
    setBusy(true);
    try {
      const path = isEdit
        ? `/api/game/turns/${encodeURIComponent(initial.id)}`
        : `/api/game/sessions/${encodeURIComponent(sessionId)}/turns`;
      await readResponse(await apiFetch(path, {
        method: isEdit ? 'PUT' : 'POST',
        body: JSON.stringify({ turn_data: turnData }),
      }));
      onSaved();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-overlay" role="presentation" onClick={() => !busy && onClose()}>
      <div className="modal-container game-modal" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}>
        <div className="modal-title">{isEdit ? '编辑 Turn' : '追加 Turn'}</div>
        <label className="game-field">
          <span>turn_data</span>
          <textarea className="game-textarea tall" value={text} onChange={(e) => setText(e.target.value)} />
        </label>
        {error && <div className="game-form-error">{error}</div>}
        <div className="modal-actions">
          <button type="button" className="modal-button cancel" onClick={onClose} disabled={busy}>取消</button>
          <button type="button" className="modal-button confirm" onClick={handleSave} disabled={busy}>{busy ? '保存中…' : '保存'}</button>
        </div>
      </div>
    </div>
  );
}

function TurnsList({ sessionId, turns, onAppend, onEditTurn, onDeleteTurn }) {
  return (
    <div className="game-turns">
      <div className="game-detail-title">Turns</div>
      {turns.length === 0 ? (
        <div className="game-empty-inline">暂无 turn 记录</div>
      ) : (
        turns.map((turn) => (
          <div className="game-turn" key={turn.id}>
            <div className="game-turn-head">
              <span>第{turn.turn_idx}轮</span>
              <span>{formatTime(turn.created_at)}</span>
            </div>
            <JsonBlock value={turn.turn_data} />
            <div className="game-actions">
              <button type="button" onClick={() => onEditTurn(turn)}>编辑</button>
              <button type="button" className="danger" onClick={() => onDeleteTurn(turn)}>删除</button>
            </div>
          </div>
        ))
      )}
      <button type="button" className="game-add-inline" onClick={() => onAppend(sessionId)}>
        <Plus size={15} aria-hidden /> 追加
      </button>
    </div>
  );
}

function SessionDetails({ session, turns, onAppend, onEditTurn, onDeleteTurn }) {
  return (
    <div className="game-details">
      <div className="game-detail-grid">
        <div>
          <div className="game-detail-title">规则 Prompt</div>
          <pre className="game-pre">{session.system_prompt || '—'}</pre>
        </div>
        <div>
          <div className="game-detail-title">配置</div>
          <JsonBlock value={session.config_json} />
        </div>
        <div>
          <div className="game-detail-title">状态</div>
          <JsonBlock value={session.state_json} empty="新游戏，尚无状态" />
        </div>
        {session.summary && (
          <div>
            <div className="game-detail-title">总结</div>
            <pre className="game-pre">{session.summary}</pre>
          </div>
        )}
      </div>
      <TurnsList
        sessionId={session.id}
        turns={turns}
        onAppend={onAppend}
        onEditTurn={onEditTurn}
        onDeleteTurn={onDeleteTurn}
      />
    </div>
  );
}

function SessionCard({ session, activeId, expanded, turns, onToggle, onActivate, onStop, onEnd, onDelete, onEdit, onAppend, onEditTurn, onDeleteTurn }) {
  const active = activeId === session.id;
  const ended = Boolean(session.ended_at);
  return (
    <article className={`game-card ${active ? 'active' : ''}`}>
      <button type="button" className="game-card-main" onClick={() => onToggle(session.id)}>
        <div>
          <div className="game-card-title-row">
            <h3>{session.display_name || session.game_type || session.id}</h3>
            {active && <span className="game-active-badge">当前</span>}
          </div>
          <div className="game-meta">
            <span>{session.game_type}</span>
            <span>{session.state_mode}</span>
            <span>{ended ? '已结束' : '进行中'}</span>
          </div>
          <div className="game-subline">
            {(session.participants || []).join('、') || '无参与者'} · {formatTime(session.created_at)}
          </div>
        </div>
        <span className="game-expand">{expanded ? '收起' : '详情'}</span>
      </button>
      <div className="game-actions">
        {active ? (
          <button type="button" onClick={() => onStop()}>停止</button>
        ) : (
          !ended && <button type="button" onClick={() => onActivate(session.id)}>激活</button>
        )}
        <button type="button" onClick={() => onEdit(session)}>编辑</button>
        {!ended && <button type="button" onClick={() => onEnd(session)}>结束</button>}
        <button type="button" className="danger" onClick={() => onDelete(session)}>删除</button>
      </div>
      {expanded && (
        <SessionDetails
          session={session}
          turns={turns || []}
          onAppend={onAppend}
          onEditTurn={onEditTurn}
          onDeleteTurn={onDeleteTurn}
        />
      )}
    </article>
  );
}

export default function Game() {
  const [tab, setTab] = useState('list');
  const [sessions, setSessions] = useState([]);
  const [activeId, setActiveId] = useState(null);
  const [turnsBySession, setTurnsBySession] = useState({});
  const [expandedId, setExpandedId] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [sessionModal, setSessionModal] = useState(null);
  const [turnModal, setTurnModal] = useState(null);
  const [confirm, setConfirm] = useState(null);
  const [confirmBusy, setConfirmBusy] = useState(false);

  const activeSession = useMemo(
    () => sessions.find((s) => s.id === activeId) || null,
    [sessions, activeId],
  );

  const loadAll = useCallback(async () => {
    setError('');
    setLoading(true);
    try {
      const [list, active] = await Promise.all([
        readResponse(await apiFetch('/api/game/sessions')),
        readResponse(await apiFetch('/api/game/active')),
      ]);
      setSessions(list || []);
      setActiveId(active?.session_id || null);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  const loadTurns = useCallback(async (sessionId) => {
    if (!sessionId) return;
    const rows = await readResponse(await apiFetch(`/api/game/sessions/${encodeURIComponent(sessionId)}/turns`));
    setTurnsBySession((prev) => ({ ...prev, [sessionId]: rows || [] }));
  }, []);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  useEffect(() => {
    if (activeSession) loadTurns(activeSession.id).catch((e) => setError(e.message));
  }, [activeSession, loadTurns]);

  const refreshAfterChange = async (sessionId) => {
    await loadAll();
    if (sessionId) await loadTurns(sessionId);
  };

  const grouped = useMemo(() => ({
    active: sessions.filter((s) => !s.ended_at),
    ended: sessions.filter((s) => s.ended_at),
  }), [sessions]);

  const toggleExpand = async (sessionId) => {
    const next = expandedId === sessionId ? null : sessionId;
    setExpandedId(next);
    if (next && !turnsBySession[next]) {
      try {
        await loadTurns(next);
      } catch (e) {
        setError(e.message);
      }
    }
  };

  const runAction = async (fn, refreshSessionId) => {
    setError('');
    try {
      await fn();
      await refreshAfterChange(refreshSessionId);
    } catch (e) {
      setError(e.message);
    }
  };

  const askConfirm = (payload) => setConfirm(payload);
  const confirmAction = async () => {
    if (!confirm?.action) return;
    setConfirmBusy(true);
    try {
      await confirm.action();
      setConfirm(null);
    } catch (e) {
      setError(e.message);
    } finally {
      setConfirmBusy(false);
    }
  };

  const handleDeleteTurn = (turn) => askConfirm({
    title: '确认删除 Turn',
    message: `删除第 ${turn.turn_idx} 轮记录？`,
    danger: true,
    action: async () => {
      await readResponse(await apiFetch(`/api/game/turns/${encodeURIComponent(turn.id)}`, { method: 'DELETE' }));
      await loadTurns(turn.session_id);
    },
  });

  const listContent = (
    <>
      {loading ? <div className="tab-loading">加载中…</div> : (
        <div className="game-sections">
          <section>
            <div className="game-section-title">进行中</div>
            {grouped.active.length === 0 ? <div className="game-empty">暂无进行中的游戏</div> : grouped.active.map((session) => (
              <SessionCard
                key={session.id}
                session={session}
                activeId={activeId}
                expanded={expandedId === session.id}
                turns={turnsBySession[session.id]}
                onToggle={toggleExpand}
                onActivate={(id) => runAction(async () => readResponse(await apiFetch('/api/game/active', { method: 'PUT', body: JSON.stringify({ session_id: id }) })), id)}
                onStop={() => runAction(async () => readResponse(await apiFetch('/api/game/active', { method: 'PUT', body: JSON.stringify({ session_id: null }) })))}
                onEnd={(row) => askConfirm({ title: '确认结束', message: `结束「${row.display_name || row.id}」？`, action: async () => { await readResponse(await apiFetch(`/api/game/sessions/${encodeURIComponent(row.id)}/end`, { method: 'POST', body: JSON.stringify({}) })); await refreshAfterChange(row.id); } })}
                onDelete={(row) => askConfirm({ title: '确认删除', message: `删除「${row.display_name || row.id}」及全部 turns？`, danger: true, action: async () => { await readResponse(await apiFetch(`/api/game/sessions/${encodeURIComponent(row.id)}`, { method: 'DELETE' })); await refreshAfterChange(); } })}
                onEdit={setSessionModal}
                onAppend={(id) => setTurnModal({ sessionId: id })}
                onEditTurn={(turn) => setTurnModal({ sessionId: session.id, turn })}
                onDeleteTurn={handleDeleteTurn}
              />
            ))}
          </section>
          <section>
            <div className="game-section-title">已结束</div>
            {grouped.ended.length === 0 ? <div className="game-empty">暂无已结束游戏</div> : grouped.ended.map((session) => (
              <SessionCard
                key={session.id}
                session={session}
                activeId={activeId}
                expanded={expandedId === session.id}
                turns={turnsBySession[session.id]}
                onToggle={toggleExpand}
                onActivate={(id) => runAction(async () => readResponse(await apiFetch('/api/game/active', { method: 'PUT', body: JSON.stringify({ session_id: id }) })), id)}
                onStop={() => runAction(async () => readResponse(await apiFetch('/api/game/active', { method: 'PUT', body: JSON.stringify({ session_id: null }) })))}
                onEnd={() => {}}
                onDelete={(row) => askConfirm({ title: '确认删除', message: `删除「${row.display_name || row.id}」及全部 turns？`, danger: true, action: async () => { await readResponse(await apiFetch(`/api/game/sessions/${encodeURIComponent(row.id)}`, { method: 'DELETE' })); await refreshAfterChange(); } })}
                onEdit={setSessionModal}
                onAppend={(id) => setTurnModal({ sessionId: id })}
                onEditTurn={(turn) => setTurnModal({ sessionId: session.id, turn })}
                onDeleteTurn={handleDeleteTurn}
              />
            ))}
          </section>
        </div>
      )}
    </>
  );

  const currentContent = activeSession ? (
    <div className="game-current">
      <SessionCard
        session={activeSession}
        activeId={activeId}
        expanded
        turns={turnsBySession[activeSession.id] || []}
        onToggle={() => {}}
        onActivate={() => {}}
        onStop={() => runAction(async () => readResponse(await apiFetch('/api/game/active', { method: 'PUT', body: JSON.stringify({ session_id: null }) })))}
        onEnd={(row) => askConfirm({ title: '确认结束', message: `结束「${row.display_name || row.id}」？`, action: async () => { await readResponse(await apiFetch(`/api/game/sessions/${encodeURIComponent(row.id)}/end`, { method: 'POST', body: JSON.stringify({}) })); await refreshAfterChange(row.id); } })}
        onDelete={(row) => askConfirm({ title: '确认删除', message: `删除「${row.display_name || row.id}」及全部 turns？`, danger: true, action: async () => { await readResponse(await apiFetch(`/api/game/sessions/${encodeURIComponent(row.id)}`, { method: 'DELETE' })); await refreshAfterChange(); } })}
        onEdit={setSessionModal}
        onAppend={(id) => setTurnModal({ sessionId: id })}
        onEditTurn={(turn) => setTurnModal({ sessionId: activeSession.id, turn })}
        onDeleteTurn={handleDeleteTurn}
      />
    </div>
  ) : (
    <div className="game-empty large">当前没有活跃游戏</div>
  );

  return (
    <div className="game-container">
      <header className="game-page-head">
        <div className="game-title-line">
          <Link className="game-back-link" to="/config" aria-label="返回助手配置">
            <ArrowLeft size={16} aria-hidden />
          </Link>
          <div className="game-title-col">
            <p className="game-kicker">GAME MODE</p>
            <h1>游戏模式</h1>
          </div>
        </div>
        <div className="game-head-actions">
          <button type="button" className="game-icon-btn" onClick={loadAll} title="刷新" aria-label="刷新">
            <RefreshCw size={16} aria-hidden />
          </button>
          <button type="button" className="game-primary-btn" onClick={() => setSessionModal({})}>
            <Plus size={16} aria-hidden /> 新建游戏
          </button>
        </div>
      </header>
      <div className="game-tabs">
        <button type="button" className={tab === 'list' ? 'active' : ''} onClick={() => setTab('list')}>游戏列表</button>
        <button type="button" className={tab === 'current' ? 'active' : ''} onClick={() => setTab('current')}>当前游戏</button>
      </div>
      {error && <div className="game-error">{error}</div>}
      <div className="game-scroll">
        {tab === 'list' ? listContent : currentContent}
      </div>
      {sessionModal !== null && (
        <SessionModal
          initial={sessionModal}
          onClose={() => setSessionModal(null)}
          onSaved={async () => {
            const id = sessionModal.id;
            setSessionModal(null);
            await refreshAfterChange(id);
          }}
        />
      )}
      {turnModal && (
        <TurnModal
          sessionId={turnModal.sessionId}
          initial={turnModal.turn}
          onClose={() => setTurnModal(null)}
          onSaved={async () => {
            const id = turnModal.sessionId;
            setTurnModal(null);
            await loadTurns(id);
          }}
        />
      )}
      {confirm && (
        <ConfirmDialog
          title={confirm.title}
          message={confirm.message}
          danger={confirm.danger}
          busy={confirmBusy}
          onCancel={() => !confirmBusy && setConfirm(null)}
          onConfirm={confirmAction}
        />
      )}
    </div>
  );
}
