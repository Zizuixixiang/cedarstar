import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom';
import {
  ArrowLeft,
  Check,
  Plus,
  RefreshCw,
  Server,
  Settings2,
  Trash2,
  Wrench,
  X,
} from 'lucide-react';
import { apiFetch } from '../apiBase';
import '../styles/mcp-manager.css';

const MASK = '••••••';

function rowId() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function enabledValue(v) {
  return Number(v || 0) === 1;
}

async function readJson(res) {
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    throw new Error(data?.detail || data?.message || `HTTP ${res.status}`);
  }
  return data;
}

function Switch({ checked, onChange, disabled = false, label }) {
  return (
    <button
      type="button"
      className={`mcp-switch ${checked ? 'is-on' : ''}`}
      onClick={(event) => {
        event.stopPropagation();
        if (!disabled) onChange(!checked, event);
      }}
      disabled={disabled}
      aria-pressed={checked}
      aria-label={label}
    >
      <span />
    </button>
  );
}

function TransportSegment({ value, onChange }) {
  return (
    <div className="mcp-segment" role="group" aria-label="传输类型">
      <button
        type="button"
        className={value === 'streamable_http' ? 'active' : ''}
        onClick={() => onChange('streamable_http')}
      >
        Streamable HTTP
      </button>
      <button
        type="button"
        className={value === 'sse' ? 'active' : ''}
        onClick={() => onChange('sse')}
      >
        SSE
      </button>
    </div>
  );
}

function normalizeServersPayload(data) {
  if (Array.isArray(data)) return data;
  if (Array.isArray(data?.data)) return data.data;
  return [];
}

function serverTransportLabel(value) {
  return value === 'sse' ? 'SSE' : 'Streamable HTTP';
}

function normalizeKeywords(value) {
  if (!Array.isArray(value)) return [];
  const out = [];
  const seen = new Set();
  for (const item of value) {
    const text = String(item || '').trim();
    const key = text.toLowerCase();
    if (!text || seen.has(key)) continue;
    seen.add(key);
    out.push(text);
  }
  return out;
}

export function McpServerList() {
  const navigate = useNavigate();
  const location = useLocation();
  const [servers, setServers] = useState([]);
  const [toolCounts, setToolCounts] = useState({});
  const [ready, setReady] = useState(false);
  const [error, setError] = useState('');
  const [cardMessages, setCardMessages] = useState(() => {
    const serverId = location.state?.mcpServerId;
    if (!serverId) return {};
    return {
      [String(serverId)]: {
        notice: location.state?.mcpNotice || '',
        syncError: location.state?.mcpError || '',
      },
    };
  });
  const [deletingId, setDeletingId] = useState(null);
  const [busyId, setBusyId] = useState(null);

  const loadServers = useCallback(async () => {
    setError('');
    try {
      const data = await readJson(await apiFetch('/api/mcp/servers'));
      const rows = normalizeServersPayload(data);
      setServers(rows);
      const pairs = await Promise.all(
        rows.map(async (server) => {
          try {
            const toolData = await readJson(
              await apiFetch(`/api/mcp/servers/${encodeURIComponent(server.id)}/tools`)
            );
            const list = Array.isArray(toolData) ? toolData : toolData?.data || [];
            return [server.id, list.length];
          } catch {
            return [server.id, 0];
          }
        })
      );
      setToolCounts(Object.fromEntries(pairs));
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载失败');
    } finally {
      setReady(true);
    }
  }, []);

  useEffect(() => {
    loadServers();
  }, [loadServers]);

  useEffect(() => {
    if (location.state?.mcpServerId) {
      window.history.replaceState({}, document.title);
    }
  }, [location.state]);

  const toggleServer = async (server, event) => {
    event.stopPropagation();
    setBusyId(server.id);
    try {
      await readJson(await apiFetch(`/api/mcp/servers/${encodeURIComponent(server.id)}/toggle`, { method: 'PATCH' }));
      await loadServers();
    } catch (e) {
      const message = e instanceof Error ? e.message : '切换失败';
      setCardMessages((prev) => ({
        ...prev,
        [String(server.id)]: { notice: '', syncError: message },
      }));
    } finally {
      setBusyId(null);
    }
  };

  const deleteServer = async (server, event) => {
    event.stopPropagation();
    if (deletingId !== server.id) {
      setDeletingId(server.id);
      return;
    }
    setBusyId(server.id);
    try {
      await readJson(await apiFetch(`/api/mcp/servers/${encodeURIComponent(server.id)}`, { method: 'DELETE' }));
      setDeletingId(null);
      await loadServers();
    } catch (e) {
      setError(e instanceof Error ? e.message : '删除失败');
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="mcp-page">
      <header className="mcp-page-head">
        <div className="mcp-title-line">
          <Link className="mcp-back-link" to="/tools" aria-label="返回工具中心">
            <ArrowLeft size={16} aria-hidden />
          </Link>
          <div>
            <p className="mcp-kicker">CUSTOM MCP</p>
            <h1>通用 MCP 管理</h1>
          </div>
        </div>
        <div className="mcp-head-actions">
          <button type="button" className="mcp-icon-btn" onClick={loadServers} aria-label="刷新">
            <RefreshCw size={16} aria-hidden />
          </button>
          <button type="button" className="mcp-primary-btn" onClick={() => navigate('/mcp/new')} aria-label="新增 MCP Server">
            <Plus size={16} aria-hidden />
            新增
          </button>
        </div>
      </header>

      {error ? <div className="mcp-alert">操作失败：{error}</div> : null}

      {ready && servers.length === 0 ? (
        <div className="mcp-empty">
          <Server size={28} aria-hidden />
          <span>暂无 MCP Server，点击右上角新增</span>
        </div>
      ) : null}

      <div className="mcp-server-list">
        {servers.map((server) => {
          const cardMsg = cardMessages[String(server.id)];
          return (
          <article
            key={server.id}
            className="mcp-server-card"
            onClick={() => navigate(`/mcp/${encodeURIComponent(server.id)}`)}
          >
            <div className="mcp-server-main">
              <div className="mcp-server-title-row">
                <h2>{server.name || '未命名 Server'}</h2>
                <div className="mcp-server-status">
                  <span className={`mcp-badge ${enabledValue(server.enabled) ? 'on' : 'off'}`}>
                    {enabledValue(server.enabled) ? '已启用' : '已关闭'}
                  </span>
                  {cardMsg?.notice ? <span className="mcp-card-hint">{cardMsg.notice}</span> : null}
                  {cardMsg?.syncError ? (
                    <span className="mcp-card-hint mcp-card-hint--error">同步失败：{cardMsg.syncError}</span>
                  ) : null}
                </div>
              </div>
              <div className="mcp-server-url">{server.url}</div>
              <div className="mcp-server-meta">
                <span>{serverTransportLabel(server.transport)}</span>
                <span>{toolCounts[server.id] || 0} 个工具</span>
                <span>ID {server.id}</span>
              </div>
            </div>
            <div className="mcp-server-actions">
              <Switch
                checked={enabledValue(server.enabled)}
                disabled={busyId === server.id}
                onChange={(_, event) => toggleServer(server, event)}
                label="切换 Server"
              />
              {deletingId === server.id ? (
                <div className="mcp-confirm">
                  <span>确认删除？</span>
                  <button type="button" onClick={(event) => deleteServer(server, event)}>确认</button>
                  <button type="button" onClick={(event) => { event.stopPropagation(); setDeletingId(null); }}>取消</button>
                </div>
              ) : (
                <button
                  type="button"
                  className="mcp-danger-icon"
                  onClick={(event) => deleteServer(server, event)}
                  aria-label="删除"
                >
                  <Trash2 size={15} aria-hidden />
                </button>
              )}
            </div>
          </article>
          );
        })}
      </div>
    </div>
  );
}

function headerRowsToJson(rows, isEdit) {
  const out = {};
  for (const row of rows) {
    const key = (row.key || '').trim();
    const value = row.value === MASK ? '' : String(row.value || '').trim();
    if (!key) continue;
    if (isEdit && !row.dirty) continue;
    if (!value) continue;
    out[key] = value;
  }
  return Object.keys(out).length ? JSON.stringify(out) : '';
}

function McpServerForm() {
  const navigate = useNavigate();
  const { serverId } = useParams();
  const isNew = !serverId || serverId === 'new';
  const [activeTab, setActiveTab] = useState('base');
  const [servers, setServers] = useState([]);
  const [server, setServer] = useState(null);
  const [tools, setTools] = useState([]);
  const [loading, setLoading] = useState(!isNew);
  const [saving, setSaving] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [form, setForm] = useState({
    enabled: true,
    name: '',
    transport: 'streamable_http',
    url: '',
    trigger_keywords: [],
    allow_idle: false,
  });
  const [headers, setHeaders] = useState([]);
  const [keywordDraft, setKeywordDraft] = useState('');

  const currentServer = useMemo(() => {
    if (isNew) return null;
    return servers.find((item) => String(item.id) === String(serverId)) || null;
  }, [isNew, serverId, servers]);

  const loadTools = useCallback(async (id) => {
    if (!id) return;
    const data = await readJson(await apiFetch(`/api/mcp/servers/${encodeURIComponent(id)}/tools`));
    setTools(Array.isArray(data) ? data : data?.data || []);
  }, []);

  const loadServer = useCallback(async () => {
    if (isNew) {
      setLoading(false);
      return;
    }
    setLoading(true);
    setError('');
    try {
      const data = await readJson(await apiFetch('/api/mcp/servers'));
      const list = normalizeServersPayload(data);
      setServers(list);
      const row = list.find((item) => String(item.id) === String(serverId));
      if (!row) throw new Error('server not found');
      setServer(row);
      setForm({
        enabled: enabledValue(row.enabled),
        name: row.name || '',
        transport: row.transport || 'streamable_http',
        url: row.url || '',
        trigger_keywords: normalizeKeywords(row.trigger_keywords),
        allow_idle: Boolean(row.allow_idle),
      });
      setHeaders([]);
      await loadTools(row.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载失败');
    } finally {
      setLoading(false);
    }
  }, [isNew, loadTools, serverId]);

  useEffect(() => {
    loadServer();
  }, [loadServer]);

  const setField = (key, value) => {
    setForm((prev) => ({ ...prev, [key]: value }));
  };

  const addHeader = () => {
    setHeaders((prev) => [...prev, { id: rowId(), key: '', value: '', dirty: !isNew }]);
  };

  const updateHeader = (id, patch) => {
    setHeaders((prev) =>
      prev.map((row) =>
        row.id === id
          ? {
              ...row,
              ...patch,
              dirty: patch.value !== undefined ? true : row.dirty,
            }
          : row
      )
    );
  };

  const removeHeader = (id) => {
    setHeaders((prev) => prev.filter((row) => row.id !== id));
  };

  const addKeyword = (raw) => {
    const text = String(raw || '').trim();
    if (!text) return;
    setForm((prev) => {
      const current = normalizeKeywords(prev.trigger_keywords);
      if (current.some((item) => item.toLowerCase() === text.toLowerCase())) {
        return prev;
      }
      return { ...prev, trigger_keywords: [...current, text] };
    });
    setKeywordDraft('');
  };

  const removeKeyword = (keyword) => {
    setForm((prev) => ({
      ...prev,
      trigger_keywords: normalizeKeywords(prev.trigger_keywords).filter(
        (item) => item.toLowerCase() !== String(keyword || '').toLowerCase()
      ),
    }));
  };

  const handleKeywordKeyDown = (event) => {
    if (event.key === 'Enter' || event.key === ',' || event.key === '，') {
      event.preventDefault();
      addKeyword(keywordDraft);
    }
    if (event.key === 'Backspace' && !keywordDraft && form.trigger_keywords.length) {
      removeKeyword(form.trigger_keywords[form.trigger_keywords.length - 1]);
    }
  };

  const syncTools = async (id) => {
    setSyncing(true);
    try {
      await readJson(await apiFetch(`/api/mcp/servers/${encodeURIComponent(id)}/sync`, { method: 'POST' }));
      await loadTools(id);
      setNotice('工具同步完成');
    } finally {
      setSyncing(false);
    }
  };

  const saveServer = async () => {
    setError('');
    setNotice('');
    const name = form.name.trim();
    const url = form.url.trim();
    if (!name) {
      setError('请填写名称');
      return;
    }
    if (!url) {
      setError('请填写 URL');
      return;
    }
    setSaving(true);
    try {
      const body = {
        enabled: form.enabled ? 1 : 0,
        name,
        transport: form.transport,
        url,
        headers: headerRowsToJson(headers, !isNew),
        trigger_keywords: normalizeKeywords(form.trigger_keywords),
        allow_idle: Boolean(form.allow_idle),
      };
      const res = await apiFetch(
        isNew ? '/api/mcp/servers' : `/api/mcp/servers/${encodeURIComponent(serverId)}`,
        {
          method: isNew ? 'POST' : 'PUT',
          body: JSON.stringify(body),
        }
      );
      const saved = await readJson(res);
      const id = saved.id || serverId;
      setNotice('保存成功，正在同步工具');
      let syncFailed = false;
      let syncErrorMessage = '';
      try {
        await syncTools(id);
      } catch (syncErr) {
        syncFailed = true;
        syncErrorMessage = syncErr instanceof Error ? syncErr.message : '同步失败';
        setNotice('保存成功，但工具同步失败');
        setError(syncErrorMessage);
      }
      if (isNew) {
        navigate('/mcp', {
          replace: true,
          state: syncFailed
            ? {
                mcpServerId: id,
                mcpNotice: 'Server 已保存，工具同步失败，可在列表中重新进入后同步或删除',
                mcpError: syncErrorMessage,
              }
            : null,
        });
      } else {
        await loadServer();
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : '保存失败');
    } finally {
      setSaving(false);
    }
  };

  const toggleTool = async (tool, kind) => {
    setError('');
    const path = kind === 'approval' ? 'approval' : 'toggle';
    try {
      await readJson(await apiFetch(`/api/mcp/tools/${encodeURIComponent(tool.id)}/${path}`, { method: 'PATCH' }));
      await loadTools(serverId);
    } catch (e) {
      setError(e instanceof Error ? e.message : '切换失败');
    }
  };

  return (
    <div className={`mcp-page${loading ? ' is-loading' : ''}`}>
      <header className="mcp-page-head">
        <div className="mcp-title-line">
          <Link className="mcp-back-link" to="/mcp" aria-label="返回 MCP 列表">
            <ArrowLeft size={16} aria-hidden />
          </Link>
          <div>
            <p className="mcp-kicker">{isNew ? 'NEW SERVER' : 'EDIT SERVER'}</p>
            <h1>{isNew ? '新增 MCP Server' : (currentServer?.name || server?.name || '编辑 MCP Server')}</h1>
          </div>
        </div>
        <button type="button" className="mcp-primary-btn" onClick={saveServer} disabled={saving || syncing}>
          <Check size={16} aria-hidden />
          {saving ? '保存中...' : '保存'}
        </button>
      </header>

      {error ? <div className="mcp-alert">操作失败：{error}</div> : null}
      {notice ? <div className="mcp-notice">{notice}</div> : null}

      <div className="mcp-tabs">
        <button type="button" className={activeTab === 'base' ? 'active' : ''} onClick={() => setActiveTab('base')}>
          基础设置
        </button>
        {!isNew && (
          <button type="button" className={activeTab === 'tools' ? 'active' : ''} onClick={() => setActiveTab('tools')}>
            工具列表
          </button>
        )}
      </div>

      {activeTab === 'base' ? (
        <section className="mcp-panel" aria-busy={loading}>
          <div className="mcp-field-row">
            <div>
              <label className="mcp-label">启用</label>
              <div className="mcp-hint">关闭后不会注册这个 server 下的工具</div>
            </div>
            <Switch checked={form.enabled} onChange={(value) => setField('enabled', value)} label="启用 Server" />
          </div>

          <label className="mcp-field">
            <span className="mcp-label">名称</span>
            <input className="mcp-input" value={form.name} onChange={(e) => setField('name', e.target.value)} placeholder="渡口 MCP" />
          </label>

          <div className="mcp-field">
            <span className="mcp-label">传输类型</span>
            <TransportSegment value={form.transport} onChange={(value) => setField('transport', value)} />
          </div>

          <label className="mcp-field">
            <span className="mcp-label">URL</span>
            <input className="mcp-input" value={form.url} onChange={(e) => setField('url', e.target.value)} placeholder="https://example.com/mcp" />
          </label>

          <div className="mcp-field">
            <span className="mcp-label">触发关键词</span>
            <div className="mcp-tag-box">
              {normalizeKeywords(form.trigger_keywords).map((keyword) => (
                <span className="mcp-tag" key={keyword}>
                  {keyword}
                  <button type="button" onClick={() => removeKeyword(keyword)} aria-label={`删除 ${keyword}`}>
                    <X size={13} aria-hidden />
                  </button>
                </span>
              ))}
              <input
                value={keywordDraft}
                onChange={(e) => {
                  const raw = e.target.value;
                  if (raw.includes(',') || raw.includes('，')) {
                    const parts = raw.split(/[,，]/);
                    parts.slice(0, -1).forEach(addKeyword);
                    setKeywordDraft(parts[parts.length - 1] || '');
                  } else {
                    setKeywordDraft(raw);
                  }
                }}
                onKeyDown={handleKeywordKeyDown}
                onBlur={() => addKeyword(keywordDraft)}
                placeholder="留空则每轮对话都注入"
              />
            </div>
          </div>

          <div className="mcp-field-row">
            <div>
              <label className="mcp-label">允许自主活动使用</label>
              <div className="mcp-hint">开启后 Sirius 自主活动时也会加载此 MCP</div>
            </div>
            <Switch checked={Boolean(form.allow_idle)} onChange={(value) => setField('allow_idle', value)} label="允许自主活动使用" />
          </div>

          <div className="mcp-field">
            <div className="mcp-field-head">
              <div>
                <span className="mcp-label">自定义请求头</span>
                <div className="mcp-hint">编辑已有 Server 时后端不回显明文；未填写新 value 的行不会更新 headers</div>
              </div>
              <button type="button" className="mcp-small-btn" onClick={addHeader}>
                <Plus size={14} aria-hidden />
                添加
              </button>
            </div>
            {headers.length === 0 ? (
              <div className="mcp-header-empty">暂无自定义请求头</div>
            ) : (
              <div className="mcp-header-list">
                {headers.map((row) => (
                  <div className="mcp-header-row" key={row.id}>
                    <input
                      className="mcp-input"
                      value={row.key}
                      onChange={(e) => updateHeader(row.id, { key: e.target.value })}
                      placeholder="Authorization"
                    />
                    <input
                      className="mcp-input"
                      value={row.value}
                      onChange={(e) => updateHeader(row.id, { value: e.target.value })}
                      onFocus={() => row.value === MASK && updateHeader(row.id, { value: '' })}
                      placeholder={isNew ? 'Bearer ...' : MASK}
                    />
                    <button type="button" className="mcp-danger-icon" onClick={() => removeHeader(row.id)} aria-label="删除请求头">
                      <X size={15} aria-hidden />
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>

          <div className="mcp-form-footer">
            <button type="button" className="mcp-secondary-btn" onClick={() => navigate('/mcp')}>取消</button>
            <button type="button" className="mcp-primary-btn" onClick={saveServer} disabled={saving || syncing}>
              {saving ? '保存中...' : syncing ? '同步中...' : '保存并同步'}
            </button>
          </div>
        </section>
      ) : (
        <section className="mcp-panel">
          <div className="mcp-tool-head">
            <div>
              <h2>工具列表</h2>
              <p>同步结果来自 MCP server 的 list_tools()</p>
            </div>
            <button type="button" className="mcp-primary-btn" onClick={() => syncTools(serverId)} disabled={syncing}>
              <RefreshCw size={15} aria-hidden />
              {syncing ? '同步中...' : '重新同步'}
            </button>
          </div>

          {tools.length === 0 ? (
            <div className="mcp-empty">
              <Wrench size={28} aria-hidden />
              <span>尚未同步，点击同步按钮获取工具列表</span>
            </div>
          ) : (
            <div className="mcp-tool-list">
              {tools.map((tool) => (
                <article className="mcp-tool-card" key={tool.id}>
                  <div className="mcp-tool-main">
                    <h3>{tool.name}</h3>
                    <p>{tool.description || '无 description'}</p>
                  </div>
                  <div className="mcp-tool-switches">
                    <label>
                      <span>启用</span>
                      <Switch checked={enabledValue(tool.enabled)} onChange={() => toggleTool(tool, 'enabled')} label="切换工具启用" />
                    </label>
                  </div>
                </article>
              ))}
            </div>
          )}
        </section>
      )}
    </div>
  );
}

export default McpServerForm;
