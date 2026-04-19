/**
 * 核心设置页面
 * API 配置管理 + Token 消耗统计
 */
import React, { useState, useEffect, useCallback, useRef } from 'react';
import { toast } from 'react-toastify';
import 'react-toastify/dist/ReactToastify.css';
import { apiFetch } from '../apiBase';
import { KeyRound, BarChart3 } from 'lucide-react';
import '../styles/settings.css';

/* ─── 工具函数 ─── */
const maskKey = (key) => {
  if (!key) return '—';
  if (key.startsWith('****')) return key; // 已脱敏
  return key.length > 4 ? '****' + key.slice(-4) : '****';
};

const CONFIG_TYPE_LABEL = {
  chat: '对话',
  summary: '摘要',
  vision: '视觉',
  stt: '语音转录',
  embedding: 'Embedding',
  search_summary: '搜索摘要',
};

const CONFIG_TYPE_CLASS = {
  chat: 'chat-type',
  summary: 'summary-type',
  vision: 'vision-type',
  stt: 'stt-type',
  embedding: 'embedding-type',
  search_summary: 'search-summary-type',
};

const EMPTY_TAB_TIP = {
  chat: '暂无对话 API 配置，点击右上角新增',
  summary: '暂无摘要 API 配置，点击右上角新增',
  vision: '暂无视觉 API 配置，点击右上角新增',
  stt: '暂无语音转录 API 配置，点击右上角新增（模型建议 whisper-1）',
  embedding: '暂无 Embedding 配置，点击右上角新增（表情包向量用硅基流动 OpenAI 兼容 /v1/embeddings）',
  search_summary: '暂无搜索摘要模型配置，点击右上角新增（用于压缩 Tavily 结果；未填时可回退已激活的摘要 API）',
};

/* ─── 骨架屏 ─── */
function SkeletonCard({ rows = 3 }) {
  return (
    <div className="sk-card">
      <div className="sk-block sk-title-bar" />
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="sk-block sk-row" style={{ width: `${85 - i * 10}%` }} />
      ))}
    </div>
  );
}

/* ─── 弹窗：新增 / 编辑 ─── */
/* onSaved 成功时传入 form.config_type，父组件据此切换 Tab 并拉取对应列表 */
function ConfigModal({ initial, personas, onClose, onSaved, configType }) {
  const isEdit = !!initial?.id;
  const [form, setForm] = useState({
    name: initial?.name || '',
    api_key: '',
    base_url: initial?.base_url || '',
    model: initial?.model || '',
    persona_id: initial?.persona_id ?? '',
    config_type: initial?.config_type || configType || 'chat',
  });
  const [showKey, setShowKey] = useState(false);
  const [modelOptions, setModelOptions] = useState(initial?.model ? [initial.model] : []);
  const [fetchingModels, setFetchingModels] = useState(false);
  const [saving, setSaving] = useState(false);

  const set = (k, v) => setForm(p => ({ ...p, [k]: v }));

  const handleFetchModels = async () => {
    if (!form.api_key || !form.base_url) {
      toast.warning('请先填写 API Key 和 Base URL');
      return;
    }
    setFetchingModels(true);
    try {
      const res = await apiFetch('/api/settings/api-configs/fetch-models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ api_key: form.api_key, base_url: form.base_url }),
      });
      const data = await res.json();
      if (data.success && Array.isArray(data.data)) {
        setModelOptions(data.data);
        toast.success(`✓ 获取到 ${data.data.length} 个模型`, { autoClose: 2000 });
      } else {
        toast.error(data.message || '获取模型列表失败');
      }
    } catch {
      toast.error('网络错误');
    } finally {
      setFetchingModels(false);
    }
  };

  const handleSave = async () => {
    if (!form.name.trim()) { toast.warning('请填写配置名称'); return; }
    if (!isEdit && !form.api_key.trim()) { toast.warning('请填写 API Key'); return; }
    if (!form.base_url.trim()) { toast.warning('请填写 Base URL'); return; }

    setSaving(true);
    try {
      const body = {
        name: form.name.trim(),
        base_url: form.base_url.trim(),
        persona_id: form.persona_id ? Number(form.persona_id) : null,
        model: form.model.trim() || null,
        config_type: form.config_type,
      };
      // 编辑时只有填写了新 key 才更新
      if (form.api_key.trim()) body.api_key = form.api_key.trim();
      if (!isEdit) body.api_key = form.api_key.trim();

      const path = isEdit
        ? `/api/settings/api-configs/${initial.id}`
        : '/api/settings/api-configs';
      const method = isEdit ? 'PUT' : 'POST';

      const res = await apiFetch(path, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (data.success) {
        toast.success(isEdit ? '✓ 配置已更新' : '✓ 配置已创建', { autoClose: 2000 });
        onSaved(form.config_type);
      } else {
        toast.error(data.message || '操作失败');
      }
    } catch {
      toast.error('网络错误');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="modal-overlay">
      <div className="modal-box">
        <div className="modal-header">
          <h3 className="modal-title">{isEdit ? '编辑配置' : '新增配置'}</h3>
          <button className="modal-close" onClick={onClose}>✕</button>
        </div>

        <div className="modal-body">
          {/* 配置类型 */}
          <div className="modal-field">
            <label className="modal-label">配置类型</label>
            <div className="type-radio-group">
              <label className="type-radio-label">
                <input type="radio" name="config_type" value="chat"
                  checked={form.config_type === 'chat'}
                  onChange={() => set('config_type', 'chat')} />
                <span className="type-radio-text">对话 API</span>
              </label>
              <label className="type-radio-label">
                <input type="radio" name="config_type" value="summary"
                  checked={form.config_type === 'summary'}
                  onChange={() => set('config_type', 'summary')} />
                <span className="type-radio-text">摘要 API</span>
              </label>
              <label className="type-radio-label">
                <input type="radio" name="config_type" value="vision"
                  checked={form.config_type === 'vision'}
                  onChange={() => set('config_type', 'vision')} />
                <span className="type-radio-text">视觉 API</span>
              </label>
              <label className="type-radio-label">
                <input type="radio" name="config_type" value="stt"
                  checked={form.config_type === 'stt'}
                  onChange={() => set('config_type', 'stt')} />
                <span className="type-radio-text">语音转录 API</span>
              </label>
              <label className="type-radio-label">
                <input type="radio" name="config_type" value="embedding"
                  checked={form.config_type === 'embedding'}
                  onChange={() => set('config_type', 'embedding')} />
                <span className="type-radio-text">Embedding</span>
              </label>
              <label className="type-radio-label">
                <input type="radio" name="config_type" value="search_summary"
                  checked={form.config_type === 'search_summary'}
                  onChange={() => set('config_type', 'search_summary')} />
                <span className="type-radio-text">搜索摘要</span>
              </label>
            </div>
            <div className="modal-hint">
              {form.config_type === 'chat' && '用于日常对话、短期记忆、思维链'}
              {form.config_type === 'summary' && '用于批量总结、记忆归档、微批处理（建议用大模型如 GPT-4）'}
              {form.config_type === 'vision' && '用于图片理解、多模态消息（与对话/摘要配置独立激活）'}
              {form.config_type === 'stt' && '用于 Telegram/Discord 语音转文字（OpenAI 兼容 /audio/transcriptions，模型填 whisper-1）'}
              {form.config_type === 'embedding' && '用于表情包 Chroma 检索（硅基流动 BAAI/bge-m3，OpenAI 兼容 /v1/embeddings）；需在列表中激活'}
              {form.config_type === 'search_summary' && '用于 web_search：把 Tavily 多条结果压成短摘要（建议小模型）；未激活本类型时回退「摘要 API」'}
            </div>
          </div>

          {/* 配置名称 */}
          <div className="modal-field">
            <label className="modal-label">配置名称</label>
            <input className="modal-input" value={form.name}
              onChange={e => set('name', e.target.value)} placeholder="如：DeepSeek 主力配置" />
          </div>

          {/* Base URL（调整到 Key 前面） */}
          <div className="modal-field">
            <label className="modal-label">Base URL</label>
            <input className="modal-input" value={form.base_url}
              onChange={e => set('base_url', e.target.value)}
              placeholder="https://api.deepseek.com" />
          </div>

          {/* API Key */}
          <div className="modal-field">
            <label className="modal-label">API Key {isEdit && <span className="modal-hint">（留空保持不变）</span>}</label>
            <div className="modal-input-wrap">
              <input className="modal-input" type={showKey ? 'text' : 'password'}
                value={form.api_key}
                onChange={e => set('api_key', e.target.value)}
                placeholder={isEdit ? '留空则不修改' : 'sk-...'} />
              <button className="eye-btn" onClick={() => setShowKey(v => !v)}>
                {showKey ? '🙈' : '👁'}
              </button>
            </div>
          </div>

          {/* 模型 */}
          <div className="modal-field">
            <label className="modal-label">模型</label>
            <div className="modal-model-row">
              {modelOptions.length > 0 ? (
                <select className="modal-select" value={form.model}
                  onChange={e => set('model', e.target.value)}>
                  <option value="">请选择模型</option>
                  {modelOptions.map(m => <option key={m} value={m}>{m}</option>)}
                </select>
              ) : (
                <input className="modal-input" value={form.model}
                  onChange={e => set('model', e.target.value)}
                  placeholder="手动输入模型名，或点右侧按钮获取" />
              )}
              <button
                className={`fetch-models-btn ${fetchingModels ? 'loading' : ''}`}
                onClick={handleFetchModels}
                disabled={fetchingModels}
              >
                {fetchingModels ? <span className="spin">⟳</span> : '获取模型列表'}
              </button>
            </div>
            {modelOptions.length > 0 && (
              <button className="clear-models-btn" onClick={() => setModelOptions([])}>
                切换为手动输入
              </button>
            )}
          </div>

          {/* 关联人设 */}
          <div className="modal-field">
            <label className="modal-label">关联人设</label>
            <select className="modal-select" value={form.persona_id}
              onChange={e => set('persona_id', e.target.value)}>
              <option value="">不关联人设</option>
              {personas.map(p => (
                <option key={p.id} value={p.id}>{p.name}</option>
              ))}
            </select>
          </div>
        </div>

        <div className="modal-footer">
          <button className="modal-btn-cancel" onClick={onClose}>取消</button>
          <button className="modal-btn-save" onClick={handleSave} disabled={saving}>
            {saving ? '保存中...' : '保存'}
          </button>
        </div>
      </div>
    </div>
  );
}

/* ─── 主页面 ─── */
function Settings() {
  const [activeTab, setActiveTab] = useState('chat'); // 'chat' | 'summary' | 'vision' | 'stt' | 'embedding' | 'search_summary'
  const activeTabRef = useRef('chat'); // 用 ref 追踪最新 activeTab，避免闭包陷阱
  const [configs, setConfigs] = useState([]);
  const [personas, setPersonas] = useState([]);
  const [tokenStats, setTokenStats] = useState(null);
  const [period, setPeriod] = useState('latest');
  const [isLoading, setIsLoading] = useState(true);
  const [modalData, setModalData] = useState(null); // null=关闭, {}=新增, {...}=编辑
  const [confirmDeleteId, setConfirmDeleteId] = useState(null); // 待确认删除的 cfg.id

  /* 切换 tab 时同步更新 ref */
  const switchTab = (tab) => {
    activeTabRef.current = tab;
    setActiveTab(tab);
  };

  /* 获取配置列表：优先用传入的 tab，否则读 ref（永远是最新值） */
  const fetchConfigs = useCallback(async (tab) => {
    const t = tab ?? activeTabRef.current;
    try {
      const res = await apiFetch(`/api/settings/api-configs?config_type=${t}`);
      const data = await res.json();
      if (data.success) {
        setConfigs(data.data || []);
      } else {
        console.error('fetchConfigs failed:', data.message);
        setConfigs([]);
      }
    } catch (error) {
      console.error('fetchConfigs network error:', error);
      setConfigs([]);
    }
  }, []);

  /* 获取 Token 统计 */
  const fetchTokenStats = useCallback(async (p) => {
    try {
      const res = await apiFetch(`/api/settings/token-usage?period=${p}`);
      const data = await res.json();
      if (data.success) setTokenStats(data.data);
    } catch { setTokenStats(null); }
  }, []);

  /* 初始并发加载 */
  useEffect(() => {
    const init = async () => {
      try {
        await Promise.all([
          fetchConfigs(activeTab),
          fetchTokenStats('latest'),
          apiFetch('/api/persona').then(r => r.json()).then(d => {
            if (d.success) setPersonas(d.data || []);
          }),
        ]);
      } finally {
        setIsLoading(false);
      }
    };
    init();
  }, []);

  /* 切换 tab 时重新拉取 */
  useEffect(() => {
    if (!isLoading) fetchConfigs(activeTab);
  }, [activeTab]);

  /* 切换 period */
  useEffect(() => {
    if (!isLoading) fetchTokenStats(period);
  }, [period]);

  /* 激活配置 */
  const handleActivate = async (id) => {
    const res = await apiFetch(`/api/settings/api-configs/${id}/activate`, { method: 'PUT' });
    const data = await res.json();
    if (data.success) {
      toast.success('✓ 已激活', { autoClose: 2000 });
      fetchConfigs(activeTabRef.current);
    }
  };

  /* 删除配置（第一次点击进入待确认态，再次点击才真正删除） */
  const handleDeleteClick = (cfg) => {
    if (cfg.is_active == 1 || cfg.is_active === true) {
      toast.warning('请先切换到其他配置再删除');
      return;
    }
    setConfirmDeleteId(cfg.id);
  };

  const handleDeleteConfirm = async (cfg) => {
    setConfirmDeleteId(null);
    try {
      const res = await apiFetch(`/api/settings/api-configs/${cfg.id}`, { method: 'DELETE' });
      const data = await res.json();
      if (data.success) {
        toast.success('✓ 已删除', { autoClose: 2000 });
        fetchConfigs(activeTabRef.current);
      } else {
        toast.error(data.message || '删除失败');
      }
    } catch {
      toast.error('网络错误，删除失败');
    }
  };

  /* 弹窗保存后：按保存的配置类型刷新；若与当前 Tab 不一致则切换 Tab（避免摘要配在对话 Tab 下仍拉 chat 列表） */
  const handleSaved = (savedConfigType) => {
    setModalData(null);
    const nextTab = ['chat', 'summary', 'vision', 'stt', 'embedding', 'search_summary'].includes(savedConfigType)
      ? savedConfigType
      : activeTabRef.current;
    if (nextTab !== activeTabRef.current) {
      switchTab(nextTab);
    } else {
      fetchConfigs(nextTab);
    }
  };

  /* Token 数值格式化 */
  const fmt = (n) => n == null ? '—' : Number(n).toLocaleString();

  /* 平台进度条：动态读取 by_platform，避免硬编码 */
  const totalTokens = tokenStats?.total_tokens || 0;
  const PLATFORM_COLOR = {
    telegram: '#5ba4cf',
    discord:  '#7289da',
    batch:    '#a0aec0',
  };
  const platforms = Object.entries(tokenStats?.by_platform || {})
    .filter(([, v]) => Number(v) > 0)
    .map(([key, value]) => ({
      label: key.charAt(0).toUpperCase() + key.slice(1),
      value: Number(value),
      color: PLATFORM_COLOR[key.toLowerCase()] || '#b794f4',
    }))
    .sort((a, b) => b.value - a.value);

  if (isLoading) {
    return (
      <div className="settings-page">
        <SkeletonCard rows={4} />
        <SkeletonCard rows={3} />
      </div>
    );
  }

  return (
    <div className="settings-page">

      {/* ① API 配置管理 */}
      <div className="settings-card">
        <div className="card-header">
          <h2 className="card-title card-title--with-icon">
            <KeyRound className="card-title-icon" strokeWidth={1.75} aria-hidden />
            API 配置管理
          </h2>
          <div className="config-tabs">
            <button 
              className={`config-tab ${activeTab === 'chat' ? 'active' : ''}`}
              onClick={() => switchTab('chat')}
            >
              对话 API
            </button>
            <button 
              className={`config-tab ${activeTab === 'summary' ? 'active' : ''}`}
              onClick={() => switchTab('summary')}
            >
              摘要 API
            </button>
            <button 
              className={`config-tab ${activeTab === 'vision' ? 'active' : ''}`}
              onClick={() => switchTab('vision')}
            >
              视觉 API
            </button>
            <button 
              className={`config-tab ${activeTab === 'stt' ? 'active' : ''}`}
              onClick={() => switchTab('stt')}
            >
              语音转录
            </button>
            <button 
              className={`config-tab ${activeTab === 'embedding' ? 'active' : ''}`}
              onClick={() => switchTab('embedding')}
            >
              Embedding
            </button>
            <button 
              className={`config-tab ${activeTab === 'search_summary' ? 'active' : ''}`}
              onClick={() => switchTab('search_summary')}
            >
              搜索摘要
            </button>
          </div>
          <button className="btn-add" onClick={() => setModalData({ config_type: activeTab })}>
            ＋ 新增配置
          </button>
        </div>

        {configs.length === 0 ? (
          <div className="empty-tip">
            {EMPTY_TAB_TIP[activeTab] || EMPTY_TAB_TIP.chat}
          </div>
        ) : (
          <div className="config-list">
            {configs.map(cfg => (
              <div key={cfg.id} className={`config-row ${cfg.is_active ? 'active-row' : ''}`}>
                {/* 左：名称 + 激活标签 */}
                <div className="cfg-left">
                  <span className="cfg-name">{cfg.name}</span>
                  <span
                    className={['cfg-type-tag', CONFIG_TYPE_CLASS[cfg.config_type]]
                      .filter(Boolean)
                      .join(' ')}
                  >
                    {CONFIG_TYPE_LABEL[cfg.config_type] || cfg.config_type}
                  </span>
                  {cfg.is_active && <span className="tag-active">激活中</span>}
                </div>
                {/* 中：URL + 人设 */}
                <div className="cfg-mid">
                  <span className="cfg-url" title={cfg.base_url}>{cfg.base_url}</span>
                  {cfg.persona_name && (
                    <span className="cfg-persona">人设：{cfg.persona_name}</span>
                  )}
                  {cfg.model && (
                    <span className="cfg-model">模型：{cfg.model}</span>
                  )}
                </div>
                {/* 右：操作 */}
                <div className="cfg-actions">
                  {confirmDeleteId === cfg.id ? (
                    <>
                      <span className="cfg-del-confirm-text">确认删除？</span>
                      <button className="btn-del-confirm" onClick={() => handleDeleteConfirm(cfg)}>确认</button>
                      <button className="btn-del-cancel" onClick={() => setConfirmDeleteId(null)}>取消</button>
                    </>
                  ) : (
                    <>
                      {!cfg.is_active && (
                        <button className="btn-activate" onClick={() => handleActivate(cfg.id)}>
                          设为激活
                        </button>
                      )}
                      <button className="btn-edit" onClick={() => setModalData(cfg)}>编辑</button>
                      <button className="btn-del" onClick={() => handleDeleteClick(cfg)}>删除</button>
                    </>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* ② Token 消耗统计 */}
      <div className="settings-card">
        <div className="card-header">
          <h2 className="card-title card-title--with-icon">
            <BarChart3 className="card-title-icon" strokeWidth={1.75} aria-hidden />
            Token 消耗统计
          </h2>
          <div className="period-tabs">
            {[['latest','本次'], ['today','今日'], ['week','本周'], ['month','本月']].map(([v, l]) => (
              <button
                key={v}
                className={`period-tab ${period === v ? 'active' : ''}`}
                onClick={() => setPeriod(v)}
              >{l}</button>
            ))}
          </div>
        </div>

        {!tokenStats || tokenStats.total_tokens === 0 ? (
          <div className="empty-tip">暂无统计数据</div>
        ) : (
          <>
            {/* 三个数字卡片 */}
            <div className="token-nums">
              <div className="token-num-card">
                <span className="token-num-val">{fmt(tokenStats.total_tokens)}</span>
                <span className="token-num-label">总消耗</span>
              </div>
              <div className="token-num-card">
                <span className="token-num-val">{fmt(tokenStats.prompt_tokens)}</span>
                <span className="token-num-label">输入消耗</span>
              </div>
              <div className="token-num-card">
                <span className="token-num-val">{fmt(tokenStats.completion_tokens)}</span>
                <span className="token-num-label">生成消耗</span>
              </div>
            </div>

            {/* 平台进度条 */}
            <div className="platform-bars">
              {platforms.map(p => {
                const pct = totalTokens > 0 ? Math.round((p.value / totalTokens) * 100) : 0;
                return (
                  <div key={p.label} className="platform-bar-row">
                    <span className="platform-label">{p.label}</span>
                    <div className="platform-bar-mid">
                      <div className="bar-track">
                        <div
                          className="bar-fill"
                          style={{ width: `${pct}%`, background: p.color }}
                        />
                      </div>
                    </div>
                    <div className="platform-stats">
                      <span className="platform-val">{fmt(p.value)}</span>
                      <span className="platform-pct">{pct}%</span>
                    </div>
                  </div>
                );
              })}
            </div>

            {/* 底部小字 */}
            <div className="token-footer">
              数据来源：共 {tokenStats.call_count ?? '—'} 次调用
            </div>
          </>
        )}
      </div>

      {/* 弹窗 */}
      {modalData !== null && (
        <ConfigModal
          initial={modalData}
          personas={personas}
          configType={activeTab}
          onClose={() => setModalData(null)}
          onSaved={handleSaved}
        />
      )}
    </div>
  );
}

export default Settings;
