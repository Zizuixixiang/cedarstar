/**
 * 核心设置页面
 * API 配置管理 + Token 消耗统计
 */
import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { toast } from 'react-toastify';
import 'react-toastify/dist/ReactToastify.css';
import { apiFetch } from '../apiBase';
import { KeyRound, BarChart3 } from 'lucide-react';
import { useHorizontalDragScroll } from '../useHorizontalDragScroll';
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
  tts: '语音合成',
  embedding: 'Embedding',
  search_summary: '搜索摘要',
  analysis: '分析',
};

const CONFIG_TYPE_CLASS = {
  chat: 'chat-type',
  summary: 'summary-type',
  vision: 'vision-type',
  stt: 'stt-type',
  tts: 'tts-type',
  embedding: 'embedding-type',
  search_summary: 'search-summary-type',
  analysis: 'analysis-type',
};

const EMPTY_TAB_TIP = {
  chat: '暂无对话 API 配置，点击右上角新增',
  summary: '暂无摘要 API 配置，点击右上角新增',
  vision: '暂无视觉 API 配置，点击右上角新增',
  stt: '暂无语音转录 API 配置，点击右上角新增（可用硅基流动 OpenAI 兼容，或选择火山引擎原生 ASR）',
  tts: '暂无语音合成 API 配置，点击右上角新增（MiniMax T2A v2，填写 API Key 即可）',
  embedding: '暂无 Embedding 配置，点击右上角新增（表情包向量用硅基流动 OpenAI 兼容 /v1/embeddings）',
  search_summary: '暂无搜索摘要模型配置，点击右上角新增（用于压缩 Tavily 结果；未填时可回退已激活的摘要 API）',
  analysis: '暂无分析模型配置，点击右上角新增（用于 Step 4 结构化提取与打分）',
};

const VOLCENGINE_STT_BASE_URL = 'https://openspeech.bytedance.com/api/v3/auc/bigmodel';
const VOLCENGINE_STT_MODEL = 'volc:volcengine_input_common';

const CHAT_LIKE_CONFIG_TYPES = new Set([
  'chat', 'summary', 'vision', 'search_summary', 'analysis',
]);

const TESTABLE_CONFIG_TYPES = new Set([...CHAT_LIKE_CONFIG_TYPES, 'embedding']);

const normBaseUrl = (url) => (url || '').trim().replace(/\/$/, '');

const applyGroupOrder = (groups, orderList) => {
  if (!orderList?.length) return groups;
  return [...groups].sort((a, b) => {
    const ia = orderList.indexOf(a.baseUrl);
    const ib = orderList.indexOf(b.baseUrl);
    if (ia === -1 && ib === -1) return 0;
    if (ia === -1) return 1;
    if (ib === -1) return -1;
    return ia - ib;
  });
};

const applyFavoriteModelOrder = (favorites, orderList) => {
  if (!orderList?.length) return favorites;
  const byModel = new Map(favorites.map((f) => [f.model, f]));
  const out = [];
  for (const model of orderList) {
    if (byModel.has(model)) out.push(byModel.get(model));
  }
  for (const f of favorites) {
    if (!orderList.includes(f.model)) out.push(f);
  }
  return out;
};

const isConfigActive = (cfg) => cfg?.is_active === 1 || cfg?.is_active === true;

const formatTestResultBody = (data, reply) => {
  const lines = [];
  if (data?.used_fixed_context) {
    const cached = data.context_cached ? '（已缓存固定抽样）' : '（本次从数据库重新抽样并缓存）';
    lines.push(
      `上下文：固定长文本约 ${data.context_char_count ?? '—'} 字${cached}，来源 ${data.source_message_count ?? '—'} 条消息`
    );
  } else if (data != null) {
    lines.push('上下文：⚠ 固定长文本未就绪（历史消息不足）');
  }
  if (data?.config_name || data?.model) {
    lines.push(`配置：${data.config_name || '—'} · 模型 ${data.model || '—'}`);
  }
  if (data?.context_build_ms != null || data?.llm_ms != null) {
    lines.push(`耗时：加载 ${data.context_build_ms ?? '—'}ms · 模型 ${data.llm_ms ?? '—'}ms`);
  }
  if (lines.length) {
    lines.push('');
  }
  lines.push(reply || '(无文本回复)');
  return lines.join('\n');
};

const buildModelChoices = (favorites, currentModel, orderList) => {
  const ordered = applyFavoriteModelOrder(favorites, orderList);
  return Array.from(
    new Set([
      ...ordered.map((f) => f.model),
      ...(currentModel ? [currentModel] : []),
    ])
  ).filter(Boolean);
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

/* ─── 测试结果块（可关闭） ─── */
function TestResultBlock({ result, onClose, className = '' }) {
  if (!result) return null;
  return (
    <div className={`cfg-test-result ${result.success ? 'cfg-test-result--ok' : 'cfg-test-result--err'} ${className}`.trim()}>
      <div className="cfg-test-result-head">
        <div className="cfg-test-result-title">{result.title}</div>
        <button type="button" className="cfg-test-result-close" onClick={onClose} aria-label="关闭">
          关闭
        </button>
      </div>
      <pre className="cfg-test-result-body">{result.body}</pre>
    </div>
  );
}

/* ─── 弹窗：新增 / 编辑 ─── */
/* onSaved 成功时传入 form.config_type，父组件据此切换 Tab 并拉取对应列表 */
function ConfigModal({
  initial,
  personas,
  onClose,
  onSaved,
  configType,
  favoriteModelOrder = {},
  onFavoriteModelOrderChange,
}) {
  const isEdit = !!initial?.id;
  const [form, setForm] = useState({
    name: initial?.name || '',
    api_key: '',
    base_url: initial?.base_url || '',
    model: initial?.model || '',
    persona_id: initial?.persona_id ?? '',
    config_type: initial?.config_type || configType || 'chat',
    voice_id: initial?.voice_id || '',
  });
  const [showKey, setShowKey] = useState(false);
  const [modelOptions, setModelOptions] = useState(initial?.model ? [initial.model] : []);
  const [favoriteModels, setFavoriteModels] = useState([]);
  const [fetchingModels, setFetchingModels] = useState(false);
  const [saving, setSaving] = useState(false);
  const [manualModelInput, setManualModelInput] = useState(false);
  const [testing, setTesting] = useState(false);
  const [modalTestResult, setModalTestResult] = useState(null);
  const [dragFavId, setDragFavId] = useState(null);
  const [modalFavOrder, setModalFavOrder] = useState(null);

  const isStt = form.config_type === 'stt';
  const isVolcengineStt = isStt && (
    form.base_url.toLowerCase().includes('openspeech') ||
    form.base_url.toLowerCase().includes('volc') ||
    form.model.toLowerCase().startsWith('volc:') ||
    form.model.toLowerCase().startsWith('volcengine:')
  );

  const set = (k, v) => setForm(p => ({ ...p, [k]: v }));

  const applyVolcengineSttPreset = () => {
    setForm(p => ({
      ...p,
      config_type: 'stt',
      name: p.name || '火山引擎语音识别',
      base_url: p.base_url || VOLCENGINE_STT_BASE_URL,
      model: p.model || VOLCENGINE_STT_MODEL,
    }));
    toast.info('已填入火山引擎 STT 预设，API Key 填写 appid:access_token 或单个 API Key');
  };

  const fetchFavorites = useCallback(async (baseUrl = form.base_url) => {
    if (!baseUrl.trim()) {
      setFavoriteModels([]);
      return;
    }
    try {
      const res = await apiFetch(`/api/settings/model-favorites?base_url=${encodeURIComponent(baseUrl.trim())}`);
      const data = await res.json();
      if (data.success) setFavoriteModels(data.data || []);
    } catch {
      setFavoriteModels([]);
    }
  }, [form.base_url]);

  useEffect(() => {
    fetchFavorites(form.base_url);
  }, [form.base_url, fetchFavorites]);

  useEffect(() => {
    setModalFavOrder(null);
  }, [form.base_url, favoriteModels]);

  const normalizeModelIds = (raw) => {
    if (!Array.isArray(raw)) return [];
    return raw
      .map((m) => (typeof m === 'string' ? m : (m?.id || m?.name || m?.model || '')))
      .map((s) => String(s).trim())
      .filter(Boolean);
  };

  const handleFetchModels = async () => {
    if (!form.base_url.trim()) {
      toast.warning('请先填写 Base URL');
      return;
    }
    if (!form.api_key.trim() && !(isEdit && initial?.id)) {
      toast.warning('请先填写 API Key');
      return;
    }
    setFetchingModels(true);
    try {
      const body = {
        base_url: form.base_url.trim(),
        api_key: form.api_key.trim(),
      };
      if (isEdit && initial?.id) {
        body.config_id = initial.id;
      }
      const res = await apiFetch('/api/settings/api-configs/fetch-models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      const ids = normalizeModelIds(data.data);
      if (data.success && ids.length > 0) {
        setModelOptions(ids);
        setManualModelInput(false);
        toast.success(`✓ 获取到 ${ids.length} 个模型`, { autoClose: 2000 });
      } else if (data.success) {
        toast.warning('接口未返回可用模型，下拉仍显示已收藏模型');
      } else {
        toast.error(data.message || '获取模型列表失败');
      }
    } catch {
      toast.error('网络错误');
    } finally {
      setFetchingModels(false);
    }
  };

  const handleFavoriteModel = async () => {
    if (!form.base_url.trim() || !form.model.trim()) {
      toast.warning('请先填写 Base URL 和模型');
      return;
    }
    const res = await apiFetch('/api/settings/model-favorites', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ base_url: form.base_url.trim(), model: form.model.trim() }),
    });
    const data = await res.json();
    if (data.success) {
      toast.success('✓ 已收藏模型', { autoClose: 1600 });
      fetchFavorites(form.base_url);
    } else {
      toast.error(data.message || '收藏失败');
    }
  };

  const handleUnfavoriteModel = async (favoriteId) => {
    if (!favoriteId) return;
    const res = await apiFetch(`/api/settings/model-favorites/${favoriteId}`, { method: 'DELETE' });
    const data = await res.json();
    if (data.success) {
      toast.success('✓ 已取消收藏', { autoClose: 1600 });
      fetchFavorites(form.base_url);
    } else {
      toast.error(data.message || '取消收藏失败');
    }
  };

  const activeFavorite = favoriteModels.find((f) => f.model === form.model);

  const handleTestConfig = async () => {
    if (!isEdit || !initial?.id) {
      toast.warning('请先保存配置后再测试');
      return;
    }
    setTesting(true);
    setModalTestResult(null);
    try {
      const res = await apiFetch(`/api/settings/api-configs/${initial.id}/test`, { method: 'POST' });
      const data = await res.json();
      const reply = data.data?.reply || '';
      const rawText = data.data?.raw ? JSON.stringify(data.data.raw, null, 2) : '';
      if (data.success) {
        setModalTestResult({
          success: true,
          title: '测试成功',
          body: formatTestResultBody(data.data, reply),
        });
        toast.success('测试成功', { autoClose: 2000 });
      } else {
        const errDetail = rawText.slice(0, 2000) || data.message || '(无详情)';
        setModalTestResult({
          success: false,
          title: '测试失败',
          body: formatTestResultBody(data.data, errDetail),
        });
        toast.error(data.message || '测试失败', { autoClose: 3000 });
      }
    } catch {
      setModalTestResult({
        success: false,
        title: '网络错误',
        body: '请检查网络或稍后重试',
      });
      toast.error('网络错误');
    } finally {
      setTesting(false);
    }
  };

  const favBaseKey = normBaseUrl(form.base_url);
  const favOrderList = modalFavOrder ?? favoriteModelOrder[favBaseKey] ?? [];
  const orderedFavoriteModels = applyFavoriteModelOrder(favoriteModels, favOrderList);

  const applyFavoriteOrder = (ordered) => {
    const order = ordered.map((f) => f.model);
    setModalFavOrder(order);
    if (onFavoriteModelOrderChange && favBaseKey) {
      onFavoriteModelOrderChange(favBaseKey, order);
    }
  };

  const reorderFavoriteModels = (fromId, toId) => {
    if (!fromId || !toId || fromId === toId || !favBaseKey) return;
    const ordered = applyFavoriteModelOrder(favoriteModels, favOrderList);
    const fromIdx = ordered.findIndex((f) => f.id === fromId);
    const toIdx = ordered.findIndex((f) => f.id === toId);
    if (fromIdx < 0 || toIdx < 0) return;
    const next = [...ordered];
    const [moved] = next.splice(fromIdx, 1);
    next.splice(toIdx, 0, moved);
    applyFavoriteOrder(next);
  };

  const moveFavoriteModel = (index, direction) => {
    const ordered = [...orderedFavoriteModels];
    const target = index + direction;
    if (target < 0 || target >= ordered.length) return;
    [ordered[index], ordered[target]] = [ordered[target], ordered[index]];
    applyFavoriteOrder(ordered);
  };

  const mergedModelOptions = Array.from(new Set([
    ...modelOptions,
    ...orderedFavoriteModels.map((x) => x.model),
    ...(form.model ? [form.model] : []),
  ].filter(Boolean)));

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
      if (form.config_type === 'tts' && form.voice_id.trim()) {
        body.voice_id = form.voice_id.trim();
      }
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
                <input type="radio" name="config_type" value="tts"
                  checked={form.config_type === 'tts'}
                  onChange={() => set('config_type', 'tts')} />
                <span className="type-radio-text">语音合成 API</span>
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
              <label className="type-radio-label">
                <input type="radio" name="config_type" value="analysis"
                  checked={form.config_type === 'analysis'}
                  onChange={() => set('config_type', 'analysis')} />
                <span className="type-radio-text">分析 API</span>
              </label>
            </div>
            <div className="modal-hint">
              {form.config_type === 'chat' && '用于日常对话、短期记忆、思维链'}
              {form.config_type === 'summary' && '用于批量总结、记忆归档、微批处理（建议用大模型如 GPT-4）'}
              {form.config_type === 'vision' && '用于图片理解、多模态消息（与对话/摘要配置独立激活）'}
              {form.config_type === 'stt' && '用于 Telegram/Discord 语音转文字；默认支持 OpenAI 兼容 /audio/transcriptions，也支持火山引擎原生 ASR 分支'}
              {form.config_type === 'tts' && '用于 Telegram 私聊语音输出（MiniMax T2A v2）；填写 API Key 和 Voice ID，激活后文字消息后会追发语音'}
              {form.config_type === 'embedding' && '用于表情包 Chroma 检索（硅基流动 BAAI/bge-m3，OpenAI 兼容 /v1/embeddings）；需在列表中激活'}
              {form.config_type === 'search_summary' && '用于 web_search：把 Tavily 多条结果压成短摘要（建议小模型）；未激活本类型时回退「摘要 API」'}
              {form.config_type === 'analysis' && '用于日终 Step 4 的事件聚类、描述与打分；未激活时 Step 4 回退摘要 API，仍不可用则使用兜底'}
            </div>
            {form.config_type === 'stt' && (
              <div className="modal-hint">
                <button className="clear-models-btn" onClick={applyVolcengineSttPreset}>填入火山引擎预设</button>
                <span> 火山分支会在 Base URL/模型名含 volc、openspeech 或模型以 volc: 开头时自动启用；硅基流动等原逻辑不受影响。</span>
              </div>
            )}
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
              placeholder={isStt ? '硅基流动填 /v1；火山填 openspeech 识别接口 URL' : 'https://api.deepseek.com'} />
            {isStt && (
              <div className="modal-hint">
                {isVolcengineStt
                  ? '当前会走火山引擎原生 ASR 分支。'
                  : '当前会走 OpenAI 兼容语音转录分支（如硅基流动 /audio/transcriptions）。'}
              </div>
            )}
          </div>

          {/* API Key */}
          <div className="modal-field">
            <label className="modal-label">API Key</label>
            <div className="modal-input-wrap">
              <input className="modal-input" type={showKey ? 'text' : 'password'}
                value={form.api_key}
                onChange={e => set('api_key', e.target.value)}
                placeholder={isVolcengineStt ? 'appid:access_token' : (isEdit ? '留空则不修改' : 'sk-...')} />
              <button className="eye-btn" onClick={() => setShowKey(v => !v)}>
                {showKey ? '🙈' : '👁'}
              </button>
            </div>
            {isVolcengineStt && (
              <div className="modal-hint">火山引擎：填写 appid:access_token（冒号分隔），或单个 API Key（UUID 格式）。从控制台 speech/new 页面获取。</div>
            )}
          </div>

          {/* TTS Voice ID */}
          {form.config_type === 'tts' && (
            <div className="modal-field">
              <label className="modal-label">Voice ID</label>
              <input className="modal-input" value={form.voice_id}
                onChange={e => set('voice_id', e.target.value)}
                placeholder="如 Chinese_Melodic_Man 或克隆音色 ID" />
              <div className="modal-hint">MiniMax 系统音色 ID 或克隆音色 ID。系统音色列表见 MiniMax 文档。</div>
            </div>
          )}

          {/* 模型 */}
          <div className="modal-field">
            <label className="modal-label">模型</label>
            {form.config_type === 'tts' ? (
              <>
                <input className="modal-input" value={form.model}
                  onChange={e => set('model', e.target.value)}
                  placeholder="speech-2.8-turbo" />
                <div className="modal-hint">可选：speech-2.8-turbo（默认）、speech-2.8-hd、speech-2.6-turbo 等</div>
              </>
            ) : (
              <>
                <div className="modal-model-row">
                  {!manualModelInput && mergedModelOptions.length > 0 ? (
                    <select className="modal-select" value={form.model}
                      onChange={e => set('model', e.target.value)}>
                      <option value="">请选择模型</option>
                      {mergedModelOptions.map(m => <option key={m} value={m}>{m}</option>)}
                    </select>
                  ) : (
                    <input className="modal-input" value={form.model}
                      onChange={e => set('model', e.target.value)}
                      placeholder={isStt ? '硅基流动填模型名；火山可填 volc:volcengine_streaming_common' : '手动输入模型名，或点右侧按钮获取'} />
                  )}
                  <button
                    className={`fetch-models-btn ${fetchingModels ? 'loading' : ''}`}
                    onClick={handleFetchModels}
                    disabled={fetchingModels}
                  >
                    {fetchingModels ? <span className="spin">⟳</span> : '获取模型列表'}
                  </button>
                </div>
                <div className="modal-model-actions modal-model-actions--split">
                  <div className="modal-model-actions-left">
                    {!manualModelInput && mergedModelOptions.length > 0 && (
                      <button
                        type="button"
                        className="clear-models-btn"
                        onClick={() => {
                          setManualModelInput(true);
                          set('model', '');
                        }}
                      >
                        切换为手动输入
                      </button>
                    )}
                    {manualModelInput && (
                      <button type="button" className="clear-models-btn" onClick={() => setManualModelInput(false)}>
                        切换为下拉选择
                      </button>
                    )}
                  </div>
                  <div className="modal-model-actions-right">
                    {activeFavorite ? (
                      <button type="button" className="clear-models-btn" onClick={() => handleUnfavoriteModel(activeFavorite.id)}>
                        取消收藏
                      </button>
                    ) : (
                      <button type="button" className="clear-models-btn" onClick={handleFavoriteModel}>
                        收藏
                      </button>
                    )}
                  </div>
                </div>
                {orderedFavoriteModels.length > 0 && (
                  <>
                    <div className="modal-fav-list" role="list" aria-label="已收藏模型（可排序）">
                      {orderedFavoriteModels.map((f, index) => (
                        <span
                          key={f.id}
                          className={`modal-fav-chip ${dragFavId === f.id ? 'modal-fav-chip--dragging' : ''}`}
                          onDragOver={(e) => {
                            e.preventDefault();
                            e.dataTransfer.dropEffect = 'move';
                          }}
                          onDrop={(e) => {
                            e.preventDefault();
                            const fromRaw = e.dataTransfer.getData('text/plain');
                            const fromId = fromRaw ? Number(fromRaw) : dragFavId;
                            reorderFavoriteModels(fromId, f.id);
                            setDragFavId(null);
                          }}
                        >
                          <span className="modal-fav-order-btns">
                            <button
                              type="button"
                              className="modal-fav-order-btn"
                              disabled={index === 0}
                              onClick={() => moveFavoriteModel(index, -1)}
                              aria-label={`${f.model} 上移`}
                            >
                              ↑
                            </button>
                            <button
                              type="button"
                              className="modal-fav-order-btn"
                              disabled={index === orderedFavoriteModels.length - 1}
                              onClick={() => moveFavoriteModel(index, 1)}
                              aria-label={`${f.model} 下移`}
                            >
                              ↓
                            </button>
                          </span>
                          <span
                            className="modal-fav-drag"
                            draggable
                            aria-hidden="true"
                            onDragStart={(e) => {
                              e.dataTransfer.setData('text/plain', String(f.id));
                              e.dataTransfer.effectAllowed = 'move';
                              setDragFavId(f.id);
                            }}
                            onDragEnd={() => setDragFavId(null)}
                          >
                            ⋮⋮
                          </span>
                          <span
                            className="modal-fav-chip-label-wrap"
                            role="button"
                            tabIndex={0}
                            title={f.model}
                            onClick={(e) => {
                              e.stopPropagation();
                              toast.info(f.model, { autoClose: 5000 });
                            }}
                            onKeyDown={(e) => {
                              if (e.key === 'Enter' || e.key === ' ') {
                                e.preventDefault();
                                toast.info(f.model, { autoClose: 5000 });
                              }
                            }}
                          >
                            <span className="modal-fav-chip-label">{f.model}</span>
                          </span>
                          <button
                            type="button"
                            className="modal-fav-chip-remove"
                            onMouseDown={(e) => e.stopPropagation()}
                            onClick={() => handleUnfavoriteModel(f.id)}
                            aria-label={`取消收藏 ${f.model}`}
                          >
                            ×
                          </button>
                        </span>
                      ))}
                    </div>
                  </>
                )}
                {isStt && (
                  <div className="modal-hint">
                    {isVolcengineStt
                      ? '火山引擎使用 Seed-ASR 2.0 BigModel，模型字段仅用于识别火山分支，无需修改。'
                      : 'OpenAI 兼容 STT 一般填写 whisper-1 或供应商提供的语音识别模型名。'}
                  </div>
                )}
              </>
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

        {modalTestResult && (
          <div className="modal-test-panel">
            <TestResultBlock
              result={modalTestResult}
              onClose={() => setModalTestResult(null)}
              className="cfg-test-result--modal"
            />
          </div>
        )}

        <div className="modal-footer">
          {isEdit && TESTABLE_CONFIG_TYPES.has(form.config_type) && (
            <button type="button" className="modal-btn-test" onClick={handleTestConfig} disabled={testing || saving}>
              {testing ? '测试中…' : '测试'}
            </button>
          )}
          <button className="modal-btn-cancel" onClick={onClose}>取消</button>
          <button className="modal-btn-save" onClick={handleSave} disabled={saving || testing}>
            {saving ? '保存中...' : '保存'}
          </button>
        </div>
      </div>
    </div>
  );
}

/* ─── 主页面 ─── */
function Settings() {
  const [activeTab, setActiveTab] = useState('chat'); // 'chat' | 'summary' | 'vision' | 'stt' | 'embedding' | 'search_summary' | 'analysis'
  const activeTabRef = useRef('chat'); // 用 ref 追踪最新 activeTab，避免闭包陷阱
  const configTabsRef = useHorizontalDragScroll();
  const periodTabsRef = useHorizontalDragScroll();
  const [configs, setConfigs] = useState([]);
  const [personas, setPersonas] = useState([]);
  const [tokenStats, setTokenStats] = useState(null);
  const [period, setPeriod] = useState('latest');
  const [isLoading, setIsLoading] = useState(true);
  const [modalData, setModalData] = useState(null); // null=关闭, {}=新增, {...}=编辑
  const [confirmDeleteId, setConfirmDeleteId] = useState(null); // 待确认删除的 cfg.id
  const [selectedConfigIds, setSelectedConfigIds] = useState({});
  const [favoritesByBaseUrl, setFavoritesByBaseUrl] = useState({});
  const [groupOrder, setGroupOrder] = useState([]);
  const [favoriteModelOrder, setFavoriteModelOrder] = useState({});
  const [testingConfigId, setTestingConfigId] = useState(null);
  const [cardTestResult, setCardTestResult] = useState(null);

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

  const loadUiPreferences = useCallback(async (tab) => {
    try {
      const res = await apiFetch(`/api/settings/ui-preferences?config_type=${encodeURIComponent(tab)}`);
      const data = await res.json();
      if (data.success) {
        setGroupOrder(Array.isArray(data.data?.group_order) ? data.data.group_order : []);
        const fo = data.data?.favorite_model_order;
        setFavoriteModelOrder(fo && typeof fo === 'object' ? fo : {});
      }
    } catch {
      setGroupOrder([]);
      setFavoriteModelOrder({});
    }
  }, []);

  useEffect(() => {
    if (!isLoading) loadUiPreferences(activeTab);
  }, [activeTab, isLoading, loadUiPreferences]);

  const loadFavoritesForConfigs = useCallback(async (configList) => {
    const urls = [...new Set(configList.map((c) => normBaseUrl(c.base_url)).filter(Boolean))];
    if (!urls.length) {
      setFavoritesByBaseUrl({});
      return;
    }
    const entries = await Promise.all(
      urls.map(async (url) => {
        try {
          const res = await apiFetch(
            `/api/settings/model-favorites?base_url=${encodeURIComponent(url)}`
          );
          const data = await res.json();
          return [url, data.success ? data.data || [] : []];
        } catch {
          return [url, []];
        }
      })
    );
    setFavoritesByBaseUrl(Object.fromEntries(entries));
  }, []);

  useEffect(() => {
    if (!isLoading) loadFavoritesForConfigs(configs);
  }, [configs, isLoading, loadFavoritesForConfigs]);

  const persistUiPreferences = useCallback(async (nextGroupOrder, nextFavOrder) => {
    try {
      await apiFetch('/api/settings/ui-preferences', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          config_type: activeTabRef.current,
          group_order: nextGroupOrder,
          favorite_model_order: nextFavOrder,
        }),
      });
    } catch (e) {
      console.error('保存 UI 偏好失败', e);
    }
  }, []);

  const refreshFavoritesForBase = useCallback(async (baseUrl) => {
    const url = normBaseUrl(baseUrl);
    if (!url) return;
    try {
      const res = await apiFetch(
        `/api/settings/model-favorites?base_url=${encodeURIComponent(url)}`
      );
      const data = await res.json();
      if (data.success) {
        setFavoritesByBaseUrl((prev) => ({ ...prev, [url]: data.data || [] }));
      }
    } catch {
      /* ignore */
    }
  }, []);

  /* 切换 period */
  useEffect(() => {
    if (!isLoading) fetchTokenStats(period);
  }, [period]);

  /* 加入激活池（同类型可多条，LLM 报错时按 id 顺序切换） */
  const handleActivate = async (id) => {
    const res = await apiFetch(`/api/settings/api-configs/${id}/activate`, { method: 'PUT' });
    const data = await res.json();
    if (data.success) {
      toast.success('✓ 已加入激活池', { autoClose: 2000 });
      fetchConfigs(activeTabRef.current);
    }
  };

  const handleDeactivate = async (id) => {
    const res = await apiFetch(`/api/settings/api-configs/${id}/deactivate`, { method: 'PUT' });
    const data = await res.json();
    if (data.success) {
      toast.success('✓ 已取消激活', { autoClose: 2000 });
      fetchConfigs(activeTabRef.current);
    }
  };

  /* 删除配置（第一次点击进入待确认态，再次点击才真正删除） */
  const handleDeleteClick = (cfg) => {
    if (cfg.is_active == 1 || cfg.is_active === true) {
      toast.warning('请先取消激活后再删除');
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
    const nextTab = ['chat', 'summary', 'vision', 'stt', 'embedding', 'search_summary', 'analysis'].includes(savedConfigType)
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
  const configGroups = useMemo(() => {
    const map = new Map();
    for (const cfg of configs) {
      const key = normBaseUrl(cfg.base_url) || '未设置 URL';
      if (!map.has(key)) map.set(key, []);
      map.get(key).push(cfg);
    }
    const groups = Array.from(map.entries()).map(([baseUrl, items]) => ({
      baseUrl,
      items: items.sort((a, b) => Number(b.is_active || 0) - Number(a.is_active || 0)),
    }));
    return applyGroupOrder(groups, groupOrder);
  }, [configs, groupOrder]);

  const handleQuickModelChange = async (cfg, model) => {
    if (!model || model === cfg.model) return;
    try {
      const res = await apiFetch(`/api/settings/api-configs/${cfg.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model }),
      });
      const data = await res.json();
      if (data.success) {
        toast.success('✓ 模型已切换', { autoClose: 1600 });
        fetchConfigs(activeTabRef.current);
      } else {
        toast.error(data.message || '切换失败');
      }
    } catch {
      toast.error('网络错误');
    }
  };

  const handleUnfavoriteOnCard = async (favoriteId, baseUrl, modelName) => {
    try {
      const res = await apiFetch(`/api/settings/model-favorites/${favoriteId}`, { method: 'DELETE' });
      const data = await res.json();
      if (!data.success) {
        toast.error(data.message || '取消收藏失败');
        return;
      }
      toast.success('✓ 已取消收藏', { autoClose: 1600 });
      await refreshFavoritesForBase(baseUrl);
      const url = normBaseUrl(baseUrl);
      const nextFavOrder = { ...favoriteModelOrder };
      if (Array.isArray(nextFavOrder[url])) {
        nextFavOrder[url] = nextFavOrder[url].filter((m) => m !== modelName);
        setFavoriteModelOrder(nextFavOrder);
        persistUiPreferences(groupOrder, nextFavOrder);
      }
    } catch {
      toast.error('网络错误');
    }
  };

  const handleTestOnCard = async (cfg) => {
    setTestingConfigId(cfg.id);
    setCardTestResult(null);
    try {
      const res = await apiFetch(`/api/settings/api-configs/${cfg.id}/test`, { method: 'POST' });
      const data = await res.json();
      const reply = data.data?.reply || '';
      const rawText = data.data?.raw ? JSON.stringify(data.data.raw, null, 2) : '';
      if (data.success) {
        setCardTestResult({
          configId: cfg.id,
          success: true,
          title: '测试成功',
          body: formatTestResultBody(data.data, reply),
        });
        toast.success('测试成功', { autoClose: 2000 });
      } else {
        const errDetail = rawText.slice(0, 2000) || data.message || '(无详情)';
        setCardTestResult({
          configId: cfg.id,
          success: false,
          title: '测试失败',
          body: formatTestResultBody(data.data, errDetail),
        });
        toast.error(data.message || '测试失败', { autoClose: 3000 });
      }
    } catch {
      setCardTestResult({
        configId: cfg.id,
        success: false,
        title: '网络错误',
        body: '请检查网络或稍后重试',
      });
      toast.error('网络错误');
    } finally {
      setTestingConfigId(null);
    }
  };

  const moveGroup = (baseUrl, direction) => {
    const keys = configGroups.map((g) => g.baseUrl);
    const idx = keys.indexOf(baseUrl);
    if (idx < 0) return;
    const target = idx + direction;
    if (target < 0 || target >= keys.length) return;
    const next = [...keys];
    [next[idx], next[target]] = [next[target], next[idx]];
    setGroupOrder(next);
    persistUiPreferences(next, favoriteModelOrder);
  };

  const selectedConfigForGroup = useCallback((group) => {
    const wanted = selectedConfigIds[group.baseUrl];
    return (
      group.items.find((cfg) => String(cfg.id) === String(wanted)) ||
      group.items.find((cfg) => cfg.is_active) ||
      group.items[0]
    );
  }, [selectedConfigIds]);

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
          <div className="config-tabs" ref={configTabsRef}>
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
              className={`config-tab ${activeTab === 'tts' ? 'active' : ''}`}
              onClick={() => switchTab('tts')}
            >
              语音合成
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
            <button
              className={`config-tab ${activeTab === 'analysis' ? 'active' : ''}`}
              onClick={() => switchTab('analysis')}
            >
              分析 API
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
            {configGroups.map((group, groupIndex) => {
              const cfg = selectedConfigForGroup(group);
              if (!cfg) return null;
              const baseKey = normBaseUrl(group.baseUrl) || group.baseUrl;
              const favs = favoritesByBaseUrl[baseKey] || [];
              const favOrder = favoriteModelOrder[baseKey] || [];
              const modelChoices = buildModelChoices(favs, cfg.model, favOrder);
              const showModelRow = cfg.config_type !== 'tts' && modelChoices.length > 0;

              return (
                <div
                  key={group.baseUrl}
                  className={`provider-group ${isConfigActive(cfg) ? 'active-provider' : ''}`}
                >
                  {group.items.length > 1 && (
                    <div className="provider-config-row">
                      <label className="provider-model-label" htmlFor={`provider-cfg-${group.baseUrl}`}>
                        切换配置
                      </label>
                      <select
                        id={`provider-cfg-${group.baseUrl}`}
                        className="provider-model-select"
                        value={cfg.id}
                        onChange={(e) => {
                          setConfirmDeleteId(null);
                          setSelectedConfigIds((prev) => ({
                            ...prev,
                            [group.baseUrl]: Number(e.target.value),
                          }));
                        }}
                      >
                        {group.items.map((item) => (
                          <option key={item.id} value={item.id}>
                            {item.name}{isConfigActive(item) ? '（激活）' : ''}
                          </option>
                        ))}
                      </select>
                    </div>
                  )}

                  {showModelRow && (
                    <div className="provider-model-row">
                      <label className="provider-model-label" htmlFor={`provider-model-${group.baseUrl}`}>
                        模型
                      </label>
                      <select
                        id={`provider-model-${group.baseUrl}`}
                        className="provider-model-select"
                        value={cfg.model || ''}
                        onChange={(e) => handleQuickModelChange(cfg, e.target.value)}
                      >
                        {modelChoices.map((m) => (
                          <option key={m} value={m}>{m}</option>
                        ))}
                      </select>
                    </div>
                  )}

                  <div className="provider-selected-config provider-selected-config--compact">
                    <div className="cfg-compact-header">
                      <div className="provider-group-order" aria-label="调整卡片顺序">
                        <button
                          type="button"
                          className="btn-order"
                          disabled={groupIndex <= 0}
                          onClick={() => moveGroup(group.baseUrl, -1)}
                          aria-label="上移"
                        >
                          ↑
                        </button>
                        <button
                          type="button"
                          className="btn-order"
                          disabled={groupIndex >= configGroups.length - 1}
                          onClick={() => moveGroup(group.baseUrl, 1)}
                          aria-label="下移"
                        >
                          ↓
                        </button>
                      </div>
                      <span className="cfg-name">{cfg.name}</span>
                      <span
                        className={['cfg-type-tag', CONFIG_TYPE_CLASS[cfg.config_type]]
                          .filter(Boolean)
                          .join(' ')}
                      >
                        {CONFIG_TYPE_LABEL[cfg.config_type] || cfg.config_type}
                      </span>
                      {isConfigActive(cfg) ? <span className="tag-active">已启用</span> : null}
                    </div>
                    <div className="cfg-compact-body">
                      {cfg.persona_name && (
                        <div className="cfg-compact-line cfg-compact-meta">人设：{cfg.persona_name}</div>
                      )}
                      {cfg.model && cfg.config_type !== 'tts' && (
                        <div className="cfg-compact-line cfg-compact-meta cfg-compact-meta--model" title={cfg.model}>
                          模型：{cfg.model}
                        </div>
                      )}
                      <div className="cfg-actions cfg-actions--compact">
                        <div className="cfg-actions-row">
                          {confirmDeleteId === cfg.id ? (
                            <>
                              <span className="cfg-del-confirm-text">确认删除？</span>
                              <button type="button" className="btn-del-confirm" onClick={() => handleDeleteConfirm(cfg)}>确认</button>
                              <button type="button" className="btn-del-cancel" onClick={() => setConfirmDeleteId(null)}>取消</button>
                            </>
                          ) : (
                            <>
                              {TESTABLE_CONFIG_TYPES.has(cfg.config_type) && (
                                <button
                                  type="button"
                                  className="btn-test"
                                  disabled={testingConfigId === cfg.id}
                                  onClick={() => handleTestOnCard(cfg)}
                                >
                                  {testingConfigId === cfg.id ? '测试中…' : '测试'}
                                </button>
                              )}
                              {isConfigActive(cfg) ? (
                                <button type="button" className="btn-deactivate" onClick={() => handleDeactivate(cfg.id)}>
                                  取消激活
                                </button>
                              ) : (
                                <button type="button" className="btn-activate" onClick={() => handleActivate(cfg.id)}>
                                  加入激活池
                                </button>
                              )}
                              <button type="button" className="btn-edit" onClick={() => setModalData(cfg)}>编辑</button>
                              <button type="button" className="btn-del" onClick={() => handleDeleteClick(cfg)}>删除</button>
                            </>
                          )}
                        </div>
                      </div>
                      <TestResultBlock
                        result={cardTestResult?.configId === cfg.id ? cardTestResult : null}
                        onClose={() => setCardTestResult(null)}
                        className="cfg-test-result--card"
                      />
                    </div>
                  </div>
                </div>
              );
            })}
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
          <div className="period-tabs" ref={periodTabsRef}>
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
          favoriteModelOrder={favoriteModelOrder}
          onFavoriteModelOrderChange={(baseKey, order) => {
            const next = { ...favoriteModelOrder, [baseKey]: order };
            setFavoriteModelOrder(next);
            persistUiPreferences(groupOrder, next);
          }}
          onClose={() => setModalData(null)}
          onSaved={handleSaved}
        />
      )}
    </div>
  );
}

export default Settings;
