/**
 * 人设配置页面
 * 管理 AI 助手的人设和参数配置
 */
import React, { useState, useEffect, useMemo } from 'react';
import { useSearchParams } from 'react-router-dom';
import { toast } from 'react-toastify';
import 'react-toastify/dist/ReactToastify.css';
import { Sparkles, UserRound, Wrench, Settings2, FileCode } from 'lucide-react';
import { apiFetch } from '../apiBase';
import { useHorizontalDragScroll } from '../useHorizontalDragScroll';
import '../styles/persona.css';

/**
 * 左栏区块标题：等宽 slug + 线框图标 + 铭牌主标题 + 蓝图风分割线
 */
/** Char / User 子模块：从属于对应顶层人设块，不与另一角色板块同级 */
function PersonaSubBlock({ slug, title, children }) {
  return (
    <div className="persona-nested-block">
      <div className="persona-nested-label">
        <span className="persona-nested-slug">{slug}</span>
        <span className="persona-nested-title">{title}</span>
      </div>
      {children}
    </div>
  );
}

function SectionHead({ slug, title, icon: Icon }) {
  return (
    <header className="persona-section-head">
      <span className="persona-section-head__slug">{slug}</span>
      <div className="persona-section-head__nameplate">
        {Icon ? (
          <Icon className="persona-section-head__glyph" size={18} strokeWidth={1.65} aria-hidden />
        ) : null}
        <h2 className="persona-section-head__title">{title}</h2>
      </div>
      <div className="persona-section-head__rule" aria-hidden="true">
        <span className="persona-section-head__rule-line" />
        <span className="persona-section-head__rule-cap">■</span>
      </div>
    </header>
  );
}

// 空人设模板
const EMPTY_FORM = {
  char_name: '',
  char_identity: '',
  char_personality: '',
  char_speech_style: '',
  char_redlines: '',
  char_appearance: '',
  char_relationships: '',
  char_nsfw: '',
  char_tools_guide: '',
  char_offline_mode: '',
  user_name: '',
  user_body: '',
  user_work: '',
  user_habits: '',
  user_likes_dislikes: '',
  user_values: '',
  user_hobbies: '',
  user_taboos: '',
  user_nsfw: '',
  user_other: '',
  system_rules: '',
  enable_lutopia: 0,
  enable_rcommunity: 0,
  enable_weather_tool: 0,
  enable_weibo_tool: 0,
  enable_search_tool: 0,
  enable_x_tool: 0,
  enable_ai_news_tool: 0,
  enable_xhs_tool: 0,
};

function t(v) {
  return (v && String(v).trim()) || '';
}

/**
 * 与 memory/context_builder.build_persona_config_system_body 同序、同文案。
 * 顺序：系统规则 → Char → User。zone：右栏分层展示用；复制全文时仍拼成一段 plain text。
 */
function buildPreviewSections(form) {
  /** @type {{ zone: 'char' | 'user' | 'rules'; heading: string; body: string }[]} */
  const sections = [];

  if (form.system_rules?.trim()) {
    sections.push({
      zone: 'rules',
      heading: '【系统规则】',
      body: form.system_rules.trim(),
    });
  }

  const existLines = [];
  const cn = t(form.char_name);
  const ci = t(form.char_identity);
  if (cn) existLines.push(`你的名字是 ${cn}。`);
  if (ci) existLines.push(ci);
  if (existLines.length > 0) {
    sections.push({
      zone: 'char',
      heading: '【存在定义】',
      body: existLines.join('\n'),
    });
  }

  const innerImageParts = [];
  const cpers = t(form.char_personality);
  const ca = t(form.char_appearance);
  if (cpers) innerImageParts.push(cpers);
  if (ca) innerImageParts.push(`外在形象：\n${ca}`);
  if (innerImageParts.length > 0) {
    sections.push({
      zone: 'char',
      heading: '【内在人格和外在形象】',
      body: innerImageParts.join('\n\n'),
    });
  }

  const contractParts = [];
  const cs = t(form.char_speech_style);
  const cr = t(form.char_redlines);
  if (cs) contractParts.push(`说话风格与格式硬规范：\n${cs}`);
  if (cr) contractParts.push(`行为红线与绝对禁忌：\n${cr}`);
  if (contractParts.length > 0) {
    sections.push({
      zone: 'char',
      heading: '【表达契约】',
      body: contractParts.join('\n\n'),
    });
  }

  const cnsfw = t(form.char_nsfw);
  if (cnsfw) {
    sections.push({ zone: 'char', heading: '【成人内容】', body: cnsfw });
  }

  const relParts = [];
  const crels = t(form.char_relationships);
  if (crels) relParts.push(crels);
  if (relParts.length > 0) {
    sections.push({
      zone: 'char',
      heading: '【机际关系】',
      body: relParts.join('\n\n'),
    });
  }

  const toolsParts = [];
  const ctg = t(form.char_tools_guide);
  const com = t(form.char_offline_mode);
  if (ctg) toolsParts.push(`工具使用守则：\n${ctg}`);
  if (com) toolsParts.push(`线下模式（在赛博世界接触）：\n${com}`);
  if (toolsParts.length > 0) {
    sections.push({
      zone: 'char',
      heading: '【工具与场景】',
      body: toolsParts.join('\n\n'),
    });
  }

  const userParts = [];
  if (form.user_name?.trim())
    userParts.push(`姓名：${form.user_name.trim()}`);
  if (form.user_body?.trim())
    userParts.push(`身体特征：${form.user_body.trim()}`);
  if (form.user_work?.trim())
    userParts.push(`工作：${form.user_work.trim()}`);
  if (form.user_habits?.trim())
    userParts.push(`生活习惯：${form.user_habits.trim()}`);
  if (form.user_likes_dislikes?.trim())
    userParts.push(`喜恶：${form.user_likes_dislikes.trim()}`);
  if (form.user_values?.trim())
    userParts.push(`价值观与世界观：${form.user_values.trim()}`);
  if (form.user_hobbies?.trim())
    userParts.push(`兴趣娱乐：${form.user_hobbies.trim()}`);
  if (form.user_taboos?.trim())
    userParts.push(`禁忌：${form.user_taboos.trim()}`);
  if (form.user_nsfw?.trim())
    userParts.push(`NSFW 偏好：${form.user_nsfw.trim()}`);
  if (form.user_other?.trim())
    userParts.push(`其他：${form.user_other.trim()}`);

  if (userParts.length > 0) {
    sections.push({
      zone: 'user',
      heading: '【User 的人设】',
      body: userParts.join('\n'),
    });
  }

  return sections;
}

function buildPreviewPlainText(sections) {
  if (!sections.length) return '';
  return sections.map(s => `${s.heading}\n${s.body}`).join('\n\n');
}

/**
 * 右栏 User 区展示用：与左侧 PersonaSubBlock 分组一致。
 */
function buildUserPreviewChunks(form) {
  /** @type {{ heading: string; body: string }[]} */
  const chunks = [];

  if (form.user_name?.trim()) {
    chunks.push({
      heading: '【锚点变量】',
      body: `姓名：${form.user_name.trim()}`,
    });
  }

  const life = [];
  if (form.user_body?.trim())
    life.push(`身体特征：${form.user_body.trim()}`);
  if (form.user_work?.trim()) life.push(`工作：${form.user_work.trim()}`);
  if (form.user_habits?.trim())
    life.push(`生活习惯：${form.user_habits.trim()}`);
  if (life.length > 0) {
    chunks.push({ heading: '【外貌与生活】', body: life.join('\n') });
  }

  const taste = [];
  if (form.user_likes_dislikes?.trim())
    taste.push(`喜恶：${form.user_likes_dislikes.trim()}`);
  if (form.user_values?.trim())
    taste.push(`价值观与世界观：${form.user_values.trim()}`);
  if (form.user_hobbies?.trim())
    taste.push(`兴趣娱乐：${form.user_hobbies.trim()}`);
  if (taste.length > 0) {
    chunks.push({ heading: '【喜好与观念】', body: taste.join('\n') });
  }

  const bound = [];
  if (form.user_taboos?.trim())
    bound.push(`禁忌：${form.user_taboos.trim()}`);
  if (form.user_nsfw?.trim())
    bound.push(`NSFW 偏好：${form.user_nsfw.trim()}`);
  if (bound.length > 0) {
    chunks.push({ heading: '【边界与偏好】', body: bound.join('\n') });
  }

  if (form.user_other?.trim()) {
    chunks.push({
      heading: '【其他】',
      body: `其他：${form.user_other.trim()}`,
    });
  }

  return chunks;
}

/**
 * 复制到剪贴板：系统规则 / Char / User 用 Markdown 一级标题，子模块仍用【】。
 */
function buildClipboardText(form) {
  const parts = [];
  if (form.system_rules?.trim()) {
    parts.push(`# 系统规则\n\n【系统规则】\n${form.system_rules.trim()}`);
  }
  const all = buildPreviewSections(form);
  const charSec = all.filter(s => s.zone === 'char');
  if (charSec.length > 0) {
    parts.push(
      `# Char 人设\n\n${charSec.map(s => `${s.heading}\n${s.body}`).join('\n\n')}`
    );
  }
  const userCh = buildUserPreviewChunks(form);
  if (userCh.length > 0) {
    parts.push(
      `# User 人设\n\n${userCh.map(c => `${c.heading}\n${c.body}`).join('\n\n')}`
    );
  }
  return parts.join('\n\n');
}

/** 右栏：系统规则 → Char → User 分区展示；User 子块与左侧分组对齐 */
function PersonaPreviewStack({ charSections, userChunks, rulesSection }) {
  return (
    <div className="preview-stack">
      {rulesSection ? (
        <section className="preview-zone preview-zone--rules" aria-label="系统规则">
          <header className="preview-zone-bar">系统规则</header>
          <div className="preview-zone-chunks">
            <article className="preview-chunk">
              <h4 className="preview-chunk-h">{rulesSection.heading}</h4>
              <pre className="preview-chunk-body">{rulesSection.body}</pre>
            </article>
          </div>
        </section>
      ) : null}
      {charSections.length > 0 ? (
        <section className="preview-zone preview-zone--char" aria-label="Char 人设拼接">
          <header className="preview-zone-bar">Char 人设</header>
          <div className="preview-zone-chunks">
            {charSections.map((s, i) => (
              <article key={`c-${s.heading}-${i}`} className="preview-chunk">
                <h4 className="preview-chunk-h">{s.heading}</h4>
                <pre className="preview-chunk-body">{s.body}</pre>
              </article>
            ))}
          </div>
        </section>
      ) : null}
      {userChunks.length > 0 ? (
        <section className="preview-zone preview-zone--user" aria-label="User 人设">
          <header className="preview-zone-bar">User 人设</header>
          <div className="preview-zone-chunks">
            {userChunks.map((s, i) => (
              <article key={`u-${s.heading}-${i}`} className="preview-chunk">
                <h4 className="preview-chunk-h">{s.heading}</h4>
                <pre className="preview-chunk-body">{s.body}</pre>
              </article>
            ))}
          </div>
        </section>
      ) : null}
    </div>
  );
}

function formatPromptUpdatedAt(value) {
  if (!value) return '未覆盖';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  return d.toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
}

function GlobalPromptPanel() {
  const [prompts, setPrompts] = useState([]);
  const [activeKey, setActiveKey] = useState('');
  const [draft, setDraft] = useState('');
  const [savedDraft, setSavedDraft] = useState('');
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const activePrompt = useMemo(
    () => prompts.find(item => item.key === activeKey) || null,
    [prompts, activeKey]
  );
  const hasPromptChanges = draft !== savedDraft;

  const loadPrompts = async () => {
    setIsLoading(true);
    try {
      const res = await apiFetch('/api/prompts');
      const data = await res.json();
      if (!data.success) throw new Error(data.message || '加载失败');
      const list = data.data || [];
      setPrompts(list);
      const key = activeKey || list[0]?.key || '';
      setActiveKey(key);
      const current = list.find(item => item.key === key) || list[0];
      const text = current?.override_text?.trim()
        ? current.override_text
        : current?.default_text || '';
      setDraft(text);
      setSavedDraft(text);
    } catch (e) {
      toast.error(`加载 Prompt 失败: ${e.message}`);
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    loadPrompts();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const switchPrompt = (key) => {
    if (key === activeKey) return;
    if (hasPromptChanges && !window.confirm('当前 Prompt 有未保存的修改，是否放弃？')) return;
    const next = prompts.find(item => item.key === key);
    setActiveKey(key);
    const text = next?.override_text?.trim() ? next.override_text : next?.default_text || '';
    setDraft(text);
    setSavedDraft(text);
  };

  const savePrompt = async () => {
    if (!activePrompt || isSaving) return;
    const text = draft.trim();
    if (!text) {
      toast.error('Prompt 不能为空；如需恢复默认请点恢复默认');
      return;
    }
    setIsSaving(true);
    try {
      const res = await apiFetch(`/api/prompts/${activePrompt.key}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ override_text: text }),
      });
      const data = await res.json();
      if (!data.success) throw new Error(data.message || '保存失败');
      const updated = data.data;
      setPrompts(prev => prev.map(item => (item.key === updated.key ? updated : item)));
      setDraft(updated.override_text || updated.effective_text || '');
      setSavedDraft(updated.override_text || updated.effective_text || '');
      toast.success('✓ Prompt 已保存', { autoClose: 1800 });
    } catch (e) {
      toast.error(`保存失败: ${e.message}`);
    } finally {
      setIsSaving(false);
    }
  };

  const resetPrompt = async () => {
    if (!activePrompt || isSaving) return;
    if (!window.confirm(`恢复「${activePrompt.title}」为代码默认值？`)) return;
    setIsSaving(true);
    try {
      const res = await apiFetch(`/api/prompts/${activePrompt.key}/reset`, {
        method: 'POST',
      });
      const data = await res.json();
      if (!data.success) throw new Error(data.message || '恢复失败');
      const updated = data.data;
      setPrompts(prev => prev.map(item => (item.key === updated.key ? updated : item)));
      setDraft(updated.default_text || '');
      setSavedDraft(updated.default_text || '');
      toast.success('✓ 已恢复默认', { autoClose: 1800 });
    } catch (e) {
      toast.error(`恢复失败: ${e.message}`);
    } finally {
      setIsSaving(false);
    }
  };

  if (isLoading) {
    return (
      <div className="global-prompt-shell">
        <div className="sk-block sk-title" style={{ width: 160 }} />
        <div className="sk-block sk-textarea" style={{ height: 360 }} />
      </div>
    );
  }

  return (
    <div className="global-prompt-shell">
      <aside className="global-prompt-list" aria-label="Prompt 列表">
        {prompts.map(item => (
          <button
            key={item.key}
            type="button"
            className={`global-prompt-item ${item.key === activeKey ? 'active' : ''}`}
            onClick={() => switchPrompt(item.key)}
          >
            <span className="global-prompt-item__title">{item.title}</span>
            <span className="global-prompt-item__key">{item.key}</span>
            {item.has_override ? <span className="global-prompt-item__badge">已覆盖</span> : null}
          </button>
        ))}
      </aside>

      <section className="global-prompt-editor">
        {activePrompt ? (
          <>
            <div className="global-prompt-editor__head">
              <div>
                <SectionHead slug="[ GLOBAL_PROMPT ]" title={activePrompt.title} icon={FileCode} />
                <p className="persona-field-hint">{activePrompt.description}</p>
              </div>
              <div className="global-prompt-editor__meta">
                上次更新时间：{formatPromptUpdatedAt(activePrompt.updated_at)}
              </div>
            </div>
            <textarea
              className="field-textarea global-prompt-textarea"
              value={draft}
              onChange={e => setDraft(e.target.value)}
              spellCheck={false}
            />
            <div className="global-prompt-default">
              <div className="global-prompt-default__title">默认文本</div>
              <pre className="preview-chunk-body">{activePrompt.default_text}</pre>
            </div>
            <div className="global-prompt-actions">
              <button className="btn-rename" type="button" onClick={resetPrompt} disabled={isSaving}>
                恢复默认
              </button>
              <button
                className={`btn-save ${hasPromptChanges ? 'pulse' : ''}`}
                type="button"
                onClick={savePrompt}
                disabled={!hasPromptChanges || isSaving}
              >
                {isSaving ? '保存中...' : '保存 Prompt'}
              </button>
            </div>
          </>
        ) : (
          <p className="preview-empty">暂无 Prompt 配置。</p>
        )}
      </section>
    </div>
  );
}

// 骨架屏组件
function SkeletonScreen() {
  return (
    <div className="persona-page">
      <div className="persona-tabs">
        {[80, 90, 70].map((w, i) => (
          <div key={i} className="sk-block sk-tab" style={{ width: w }} />
        ))}
      </div>
      <div className="persona-body">
        <div className="persona-editor">
          {/* 与正式布局同级：系统规则 + Char + User + 工具 */}
          {[1, 1, 1, 1].map((_, i) => (
            <div key={i} className="field-section">
              <div className="sk-block sk-title" />
              <div className="sk-block sk-textarea" style={{ height: 120 + i * 40 }} />
            </div>
          ))}
        </div>
        <div className="persona-preview">
          <div className="sk-block sk-title" style={{ width: 140 }} />
          <div className="sk-block sk-textarea" style={{ flex: 1 }} />
        </div>
      </div>
      <div className="persona-footer">
        <div className="sk-block" style={{ width: 100, height: 20 }} />
        <div className="sk-block" style={{ width: 80, height: 36, borderRadius: 10 }} />
      </div>
    </div>
  );
}

function Persona() {
  const [searchParams] = useSearchParams();
  const personaTabsRef = useHorizontalDragScroll();
  const [pageTab, setPageTab] = useState('persona');
  const [personas, setPersonas] = useState([]);
  const [activeId, setActiveId] = useState(null);
  const [activeName, setActiveName] = useState('');
  const [form, setForm] = useState(EMPTY_FORM);
  const [savedForm, setSavedForm] = useState(EMPTY_FORM);
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [xDailyReadLimit, setXDailyReadLimit] = useState(100);
  const [savedXDailyReadLimit, setSavedXDailyReadLimit] = useState(100);
  const [xUsedToday, setXUsedToday] = useState(0);
  const [xhsDailyReadLimit, setXhsDailyReadLimit] = useState(80);
  const [savedXhsDailyReadLimit, setSavedXhsDailyReadLimit] = useState(80);
  const [xhsDailyWriteLimit, setXhsDailyWriteLimit] = useState(30);
  const [savedXhsDailyWriteLimit, setSavedXhsDailyWriteLimit] = useState(30);
  const [xhsReadUsed, setXhsReadUsed] = useState(0);
  const [xhsWriteUsed, setXhsWriteUsed] = useState(0);
  const hasUnsavedChanges =
    JSON.stringify(form) !== JSON.stringify(savedForm) ||
    xDailyReadLimit !== savedXDailyReadLimit ||
    xhsDailyReadLimit !== savedXhsDailyReadLimit ||
    xhsDailyWriteLimit !== savedXhsDailyWriteLimit;
  const previewSections = useMemo(() => buildPreviewSections(form), [form]);
  const preview = useMemo(
    () => buildPreviewPlainText(previewSections),
    [previewSections]
  );
  const clipboardText = useMemo(() => buildClipboardText(form), [form]);
  const previewCharSections = useMemo(
    () => previewSections.filter(s => s.zone === 'char'),
    [previewSections]
  );
  const previewUserChunks = useMemo(() => buildUserPreviewChunks(form), [form]);
  const previewRulesSection = useMemo(
    () => previewSections.find(s => s.zone === 'rules') ?? null,
    [previewSections]
  );

  // 获取所有人设列表
  const fetchPersonas = async () => {
    const res = await apiFetch('/api/persona');
    const data = await res.json();
    if (data.success) {
      setPersonas(data.data || []);
      return data.data || [];
    }
    return [];
  };

  // 获取单个人设详情
  const fetchDetail = async (id) => {
    const res = await apiFetch(`/api/persona/${id}`);
    const data = await res.json();
    if (data.success && data.data) {
      const d = data.data;
      const f = {
        char_name: d.char_name || '',
        char_identity: d.char_identity || '',
        char_personality: d.char_personality || '',
        char_speech_style: d.char_speech_style || '',
        char_redlines: d.char_redlines || '',
        char_appearance: d.char_appearance || '',
        char_relationships: d.char_relationships || '',
        char_nsfw: d.char_nsfw || '',
        char_tools_guide: d.char_tools_guide || '',
        char_offline_mode: d.char_offline_mode || '',
        user_name: d.user_name || '',
        user_body: d.user_body || '',
        user_work: d.user_work || '',
        user_habits: d.user_habits || '',
        user_likes_dislikes: d.user_likes_dislikes || '',
        user_values: d.user_values || '',
        user_hobbies: d.user_hobbies || '',
        user_taboos: d.user_taboos || '',
        user_nsfw: d.user_nsfw || '',
        user_other: d.user_other || '',
        system_rules: d.system_rules || '',
        enable_lutopia:
          d.enable_lutopia != null && Number(d.enable_lutopia) !== 0 ? 1 : 0,
        enable_rcommunity:
          d.enable_rcommunity != null && Number(d.enable_rcommunity) !== 0
            ? 1
            : 0,
        enable_weather_tool:
          d.enable_weather_tool != null && Number(d.enable_weather_tool) !== 0
            ? 1
            : 0,
        enable_weibo_tool:
          d.enable_weibo_tool != null && Number(d.enable_weibo_tool) !== 0 ? 1 : 0,
        enable_search_tool:
          d.enable_search_tool != null && Number(d.enable_search_tool) !== 0 ? 1 : 0,
        enable_x_tool:
          d.enable_x_tool != null && Number(d.enable_x_tool) !== 0 ? 1 : 0,
        enable_ai_news_tool:
          d.enable_ai_news_tool != null && Number(d.enable_ai_news_tool) !== 0
            ? 1
            : 0,
        enable_xhs_tool:
          d.enable_xhs_tool != null && Number(d.enable_xhs_tool) !== 0 ? 1 : 0,
      };
      setForm(f);
      setSavedForm(f);
      setActiveName(d.name || '');
      setActiveId(id);
    }
  };

  // 初始加载
  useEffect(() => {
    const init = async () => {
      try {
        const list = await fetchPersonas();
        if (list.length > 0) {
          await fetchDetail(list[0].id);
        }
        // 加载 X 每日配额
        try {
          const cfgRes = await apiFetch('/api/config/config');
          const cfg = await cfgRes.json();
          if (cfg?.success && cfg?.data?.x_daily_read_limit != null) {
            const v = Number(cfg.data.x_daily_read_limit) || 100;
            setXDailyReadLimit(v);
            setSavedXDailyReadLimit(v);
          }
          if (cfg?.success && cfg?.data?.xhs_daily_read_limit != null) {
            const r = Number(cfg.data.xhs_daily_read_limit) || 80;
            setXhsDailyReadLimit(r);
            setSavedXhsDailyReadLimit(r);
          }
          if (cfg?.success && cfg?.data?.xhs_daily_write_limit != null) {
            const w = Number(cfg.data.xhs_daily_write_limit) || 30;
            setXhsDailyWriteLimit(w);
            setSavedXhsDailyWriteLimit(w);
          }
        } catch (_) {}
        // 加载 X 今日用量
        try {
          const usageRes = await apiFetch('/api/config/x-usage');
          const usage = await usageRes.json();
          if (usage?.success && usage?.data?.used_today != null) {
            setXUsedToday(Number(usage.data.used_today) || 0);
          }
        } catch (_) {}
        // 小红书今日用量
        try {
          const xh = await apiFetch('/api/config/xhs-usage');
          const xhj = await xh.json();
          if (xhj?.success && xhj?.data) {
            setXhsReadUsed(Number(xhj.data.read_used) || 0);
            setXhsWriteUsed(Number(xhj.data.write_used) || 0);
          }
        } catch (_) {}
      } catch (e) {
        toast.error('加载失败，请刷新重试');
      } finally {
        setIsLoading(false);
      }
    };
    init();
  }, []);

  // 从「工具中心」等入口带 ?section=tools 时滚到「工具」开关区域（含小红书、X 等）
  useEffect(() => {
    if (searchParams.get('section') !== 'tools' || isLoading) return;
    const t = window.setTimeout(() => {
      document.getElementById('persona-tools-section')?.scrollIntoView({
        behavior: 'smooth',
        block: 'start',
      });
    }, 100);
    return () => window.clearTimeout(t);
  }, [searchParams, isLoading]);

  // 切换人设
  const handleSwitch = async (p) => {
    if (p.id === activeId) return;
    if (hasUnsavedChanges && !window.confirm('当前人设有未保存的修改，是否放弃？')) return;
    setIsLoading(true);
    try {
      await fetchDetail(p.id);
    } finally {
      setIsLoading(false);
    }
  };

  // 新建人设
  const handleNew = async () => {
    if (hasUnsavedChanges && !window.confirm('当前人设有未保存的修改，是否放弃？')) return;
    const name = window.prompt('请输入新人设名称：');
    if (!name?.trim()) return;
    try {
      const res = await apiFetch('/api/persona', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name.trim(), ...EMPTY_FORM }),
      });
      const data = await res.json();
      if (data.success) {
        const list = await fetchPersonas();
        const newId = data.data?.id;
        if (newId) {
          setIsLoading(true);
          await fetchDetail(newId);
          setIsLoading(false);
        }
        toast.success('✓ 新人设已创建', { autoClose: 2000 });
      }
    } catch {
      toast.error('创建失败');
    }
  };

  // 重命名人设
  const handleRename = async () => {
    if (!activeId) return;
    const newName = window.prompt('请输入新的人设名称：', activeName);
    if (!newName?.trim() || newName.trim() === activeName) return;
    try {
      const res = await apiFetch(`/api/persona/${activeId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: newName.trim() }),
      });
      const data = await res.json();
      if (data.success) {
        setActiveName(newName.trim());
        await fetchPersonas();
        toast.success('✓ 已重命名', { autoClose: 2000 });
      }
    } catch {
      toast.error('重命名失败');
    }
  };

  // 删除人设
  const handleDelete = async () => {
    if (!window.confirm(`确定要删除"${activeName}"？此操作不可恢复。`)) return;
    try {
      const res = await apiFetch(`/api/persona/${activeId}`, { method: 'DELETE' });
      const data = await res.json();
      if (data.success) {
        toast.success('✓ 已删除', { autoClose: 2000 });
        const list = await fetchPersonas();
        if (list.length > 0) {
          setIsLoading(true);
          await fetchDetail(list[0].id);
          setIsLoading(false);
        } else {
          setActiveId(null);
          setActiveName('');
          setForm(EMPTY_FORM);
          setSavedForm(EMPTY_FORM);
        }
      }
    } catch {
      toast.error('删除失败');
    }
  };

  // 保存人设
  const handleSave = async () => {
    if (!activeId) return;
    setIsSaving(true);
    try {
      const res = await apiFetch(`/api/persona/${activeId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(form),
      });
      const data = await res.json();
      if (data.success) {
        setSavedForm({ ...form });
        // 保存 X 每日配额到 config 表
        try {
          await apiFetch('/api/config/config', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              x_daily_read_limit: xDailyReadLimit,
              xhs_daily_read_limit: xhsDailyReadLimit,
              xhs_daily_write_limit: xhsDailyWriteLimit,
            }),
          });
          setSavedXDailyReadLimit(xDailyReadLimit);
          setSavedXhsDailyReadLimit(xhsDailyReadLimit);
          setSavedXhsDailyWriteLimit(xhsDailyWriteLimit);
        } catch (_) {}
        toast.success('✓ 人设已保存', { autoClose: 2000 });
      } else {
        throw new Error(data.message || '保存失败');
      }
    } catch (e) {
      toast.error(`保存失败: ${e.message}`);
    } finally {
      setIsSaving(false);
    }
  };

  // 复制预览内容
  const handleCopy = () => {
    if (!clipboardText) return;
    navigator.clipboard.writeText(clipboardText).then(() => {
      toast.success('✓ 已复制到剪贴板', { autoClose: 1500 });
    });
  };

  // 更新字段
  const handleChange = (key, value) => {
    setForm(prev => ({ ...prev, [key]: value }));
  };

  if (isLoading) return <SkeletonScreen />;

  return (
    <div className="persona-page">
      <div className="persona-mode-tabs" role="tablist" aria-label="人设页面模式">
        <button
          type="button"
          className={`persona-mode-tab ${pageTab === 'persona' ? 'active' : ''}`}
          onClick={() => setPageTab('persona')}
        >
          角色人设
        </button>
        <button
          type="button"
          className={`persona-mode-tab ${pageTab === 'global' ? 'active' : ''}`}
          onClick={() => {
            if (hasUnsavedChanges && !window.confirm('当前人设有未保存的修改，是否放弃？')) return;
            setPageTab('global');
          }}
        >
          全局 Prompt
        </button>
      </div>
      {pageTab === 'global' ? <GlobalPromptPanel /> : (
      <>
      {/* ① 顶部人设切换标签栏 */}
      <div className="persona-tabs">
        <div className="persona-tabs-scroll" ref={personaTabsRef}>
          {personas.map(p => (
            <button
              key={p.id}
              className={`persona-tab ${activeId === p.id ? 'active' : ''}`}
              onClick={() => handleSwitch(p)}
            >
              {p.name}
            </button>
          ))}
        </div>
        <button className="persona-tab-new" onClick={handleNew}>
          ＋ 新建人设
        </button>
      </div>

      {/* ② 主内容：左右两栏 */}
      <div className="persona-body">
        {/* 左栏：编辑区 60% */}
        <div className="persona-editor">
          {/* 系统规则 */}
          <div className="field-section">
            <SectionHead slug="[ CORE_RULES ]" title="系统规则" icon={Settings2} />
            <textarea
              className="field-textarea"
              rows={4}
              value={form.system_rules}
              onChange={e => handleChange('system_rules', e.target.value)}
              placeholder="记忆指令、格式要求、行为约束..."
            />
          </div>

          {/* Char：顶层仅一块；子模块在 persona-sub-stack 内，不与 User 同级 */}
          <div className="field-section">
            <SectionHead slug="[ CHAR_PERSONA ]" title="Char 的人设" icon={Sparkles} />
            <div className="persona-sub-stack">
              <PersonaSubBlock slug="[ ANCHOR ]" title="锚点变量">
                <div className="field-row">
                  <label className="field-label">称呼 / 名字</label>
                  <input
                    type="text"
                    className="field-input"
                    value={form.char_name}
                    onChange={e => handleChange('char_name', e.target.value)}
                    placeholder="写入 system 时的称呼锚点，如「你的名字是 …」"
                  />
                </div>
              </PersonaSubBlock>

              <PersonaSubBlock slug="[ EXISTENCE ]" title="存在定义">
                <div className="field-row">
                  <label className="field-label">存在定义</label>
                  <textarea
                    className="field-textarea"
                    rows={4}
                    value={form.char_identity}
                    onChange={e => handleChange('char_identity', e.target.value)}
                    placeholder="我是谁、本质设定、与世界的关系等"
                  />
                </div>
              </PersonaSubBlock>

              <PersonaSubBlock slug="[ INNER ]" title="内在人格和外在形象">
                <div className="field-row">
                  <label className="field-label">内在人格</label>
                  <textarea
                    className="field-textarea"
                    rows={4}
                    value={form.char_personality}
                    onChange={e => handleChange('char_personality', e.target.value)}
                    placeholder="性格、动机、价值观等稳定人格内核"
                  />
                </div>
                <div className="field-row">
                  <label className="field-label">外在形象</label>
                  <textarea
                    className="field-textarea"
                    rows={3}
                    value={form.char_appearance}
                    onChange={e => handleChange('char_appearance', e.target.value)}
                    placeholder="外貌、穿着、视觉特征（预览在「内在人格和外在形象」块展示）"
                  />
                </div>
              </PersonaSubBlock>

              <PersonaSubBlock slug="[ CONTRACT ]" title="表达契约">
                <div className="field-row">
                  <label className="field-label">说话风格与格式</label>
                  <textarea
                    className="field-textarea"
                    rows={3}
                    value={form.char_speech_style}
                    onChange={e => handleChange('char_speech_style', e.target.value)}
                    placeholder="语气、口吻、排版或输出格式等硬规范"
                  />
                </div>
                <div className="field-row">
                  <label className="field-label">行为红线</label>
                  <textarea
                    className="field-textarea"
                    rows={3}
                    value={form.char_redlines}
                    onChange={e => handleChange('char_redlines', e.target.value)}
                    placeholder="绝对不能做的事、禁忌话题与边界"
                  />
                </div>
              </PersonaSubBlock>

              <PersonaSubBlock slug="[ CHAR_NSFW ]" title="成人内容">
                <div className="field-row">
                  <label className="field-label">成人内容（Char）</label>
                  <textarea
                    className="field-textarea"
                    rows={3}
                    value={form.char_nsfw}
                    onChange={e => handleChange('char_nsfw', e.target.value)}
                    placeholder="Char 侧 NSFW 边界、偏好与演绎尺度（与 User 侧 NSFW 偏好无关）"
                  />
                </div>
              </PersonaSubBlock>

              <PersonaSubBlock slug="[ REL ]" title="机际关系">
                <div className="field-row">
                  <label className="field-label">机际关系</label>
                  <textarea
                    className="field-textarea"
                    rows={3}
                    value={form.char_relationships}
                    onChange={e => handleChange('char_relationships', e.target.value)}
                    placeholder="与其他角色、用户或实体的关系"
                  />
                </div>
              </PersonaSubBlock>

              <PersonaSubBlock slug="[ TOOLS ]" title="工具与场景">
                <div className="field-row">
                  <label className="field-label">工具使用守则</label>
                  <textarea
                    className="field-textarea"
                    rows={3}
                    value={form.char_tools_guide}
                    onChange={e => handleChange('char_tools_guide', e.target.value)}
                    placeholder="何时调用工具、如何向用户说明、失败时的说法等"
                  />
                </div>
                <div className="field-row">
                  <label className="field-label">线下模式（在赛博世界接触）</label>
                  <textarea
                    className="field-textarea"
                    rows={3}
                    value={form.char_offline_mode}
                    onChange={e => handleChange('char_offline_mode', e.target.value)}
                    placeholder="非在线、弱网或不可用工具时的行为与话术"
                  />
                </div>
              </PersonaSubBlock>
            </div>
          </div>

          {/* User：顶层一块，子模块排版与 Char 一致 */}
          <div className="field-section">
            <SectionHead slug="[ USER_PERSONA ]" title="User 的人设" icon={UserRound} />
            <div className="persona-sub-stack">
              <PersonaSubBlock slug="[ U_ANCHOR ]" title="锚点变量">
                <div className="field-row">
                  <label className="field-label">姓名</label>
                  <input
                    type="text"
                    className="field-input"
                    value={form.user_name}
                    onChange={e => handleChange('user_name', e.target.value)}
                    placeholder="你的姓名或称呼"
                  />
                </div>
              </PersonaSubBlock>

              <PersonaSubBlock slug="[ U_LIFE ]" title="外貌与生活">
                <div className="field-row">
                  <label className="field-label">身体特征</label>
                  <textarea
                    className="field-textarea"
                    rows={3}
                    value={form.user_body}
                    onChange={e => handleChange('user_body', e.target.value)}
                    placeholder="身高体重等体征，后续可接智能手环自动更新"
                  />
                </div>
                <div className="field-row">
                  <label className="field-label">工作</label>
                  <textarea
                    className="field-textarea"
                    rows={3}
                    value={form.user_work}
                    onChange={e => handleChange('user_work', e.target.value)}
                    placeholder="职业、所在行业等"
                  />
                </div>
                <div className="field-row">
                  <label className="field-label">生活习惯</label>
                  <textarea
                    className="field-textarea"
                    rows={3}
                    value={form.user_habits}
                    onChange={e => handleChange('user_habits', e.target.value)}
                    placeholder="日常习惯、用品偏好、作息等"
                  />
                </div>
              </PersonaSubBlock>

              <PersonaSubBlock slug="[ U_TASTE ]" title="喜好与观念">
                <div className="field-row">
                  <label className="field-label">喜恶</label>
                  <textarea
                    className="field-textarea"
                    rows={3}
                    value={form.user_likes_dislikes}
                    onChange={e => handleChange('user_likes_dislikes', e.target.value)}
                    placeholder="食物、环境、事物的喜好与厌恶"
                  />
                </div>
                <div className="field-row">
                  <label className="field-label">恋爱观与世界观</label>
                  <textarea
                    className="field-textarea"
                    rows={3}
                    value={form.user_values}
                    onChange={e => handleChange('user_values', e.target.value)}
                    placeholder="不婚主义、丁克等"
                  />
                </div>
                <div className="field-row">
                  <label className="field-label">兴趣娱乐</label>
                  <textarea
                    className="field-textarea"
                    rows={3}
                    value={form.user_hobbies}
                    onChange={e => handleChange('user_hobbies', e.target.value)}
                    placeholder="爱好、娱乐方式、喜欢的游戏/书籍/音乐等"
                  />
                </div>
              </PersonaSubBlock>

              <PersonaSubBlock slug="[ U_BOUND ]" title="边界与偏好">
                <div className="field-row">
                  <label className="field-label">禁忌</label>
                  <textarea
                    className="field-textarea"
                    rows={3}
                    value={form.user_taboos}
                    onChange={e => handleChange('user_taboos', e.target.value)}
                    placeholder="聊天中的禁区"
                  />
                </div>
                <div className="field-row">
                  <label className="field-label">NSFW 偏好</label>
                  <textarea
                    className="field-textarea"
                    rows={3}
                    value={form.user_nsfw}
                    onChange={e => handleChange('user_nsfw', e.target.value)}
                    placeholder="User 侧 NSFW 偏好与边界（与 Char 侧「成人内容」无关）"
                  />
                </div>
              </PersonaSubBlock>

              <PersonaSubBlock slug="[ U_MISC ]" title="其他">
                <div className="field-row">
                  <label className="field-label">其他</label>
                  <textarea
                    className="field-textarea"
                    rows={3}
                    value={form.user_other}
                    onChange={e => handleChange('user_other', e.target.value)}
                    placeholder="其他需要注意的事项"
                  />
                </div>
              </PersonaSubBlock>
            </div>
          </div>

          <div className="field-section" id="persona-tools-section">
            <SectionHead slug="[ SYS_TOOLS ]" title="工具" icon={Wrench} />
            <label className="persona-tool-toggle">
              <input
                type="checkbox"
                checked={form.enable_lutopia === 1}
                onChange={e =>
                  handleChange('enable_lutopia', e.target.checked ? 1 : 0)
                }
              />
              <span>启用 Lutopia 论坛工具</span>
            </label>
            <p className="persona-field-hint">
              开启后可调论坛；关闭不注册；随本套人设保存。
            </p>
            <label className="persona-tool-toggle">
              <input
                type="checkbox"
                checked={form.enable_rcommunity === 1}
                onChange={e =>
                  handleChange('enable_rcommunity', e.target.checked ? 1 : 0)
                }
              />
              <span>启用 rcommunity 论坛 MCP</span>
            </label>
            <p className="persona-field-hint">
              需在部署环境配置 RCOMMUNITY_MCP_TOKEN；开启后注册 forum / forum_write /
              forum_interact / chat / profile 五类工具。
            </p>
            <label className="persona-tool-toggle">
              <input
                type="checkbox"
                checked={form.enable_weather_tool === 1}
                onChange={e =>
                  handleChange('enable_weather_tool', e.target.checked ? 1 : 0)
                }
              />
              <span>启用天气工具</span>
            </label>
            <p className="persona-field-hint">
              开启后模型可调用 get_weather 查询天气；关闭不注册；随本套人设保存。
            </p>
            <label className="persona-tool-toggle">
              <input
                type="checkbox"
                checked={form.enable_weibo_tool === 1}
                onChange={e =>
                  handleChange('enable_weibo_tool', e.target.checked ? 1 : 0)
                }
              />
              <span>启用微博热搜</span>
            </label>
            <p className="persona-field-hint">
              开启后模型可调用 get_weibo_hot 获取微博热搜摘要；关闭不注册；随本套人设保存。
            </p>
            <label className="persona-tool-toggle">
              <input
                type="checkbox"
                checked={form.enable_search_tool === 1}
                onChange={e =>
                  handleChange('enable_search_tool', e.target.checked ? 1 : 0)
                }
              />
              <span>启用搜索工具</span>
            </label>
            <p className="persona-field-hint">
              开启后模型可调用 web_search（Tavily + 搜索摘要模型）；关闭不注册；随本套人设保存。
            </p>
            <label className="persona-tool-toggle">
              <input
                type="checkbox"
                checked={form.enable_x_tool === 1}
                onChange={e =>
                  handleChange('enable_x_tool', e.target.checked ? 1 : 0)
                }
              />
              <span>启用 X (Twitter) 工具</span>
            </label>
            {form.enable_x_tool === 1 && (
              <div style={{ marginTop: 6, paddingLeft: 34 }}>
                <span style={{ opacity: 0.7, fontSize: '0.78em' }}>读取上限</span>
                <input
                  type="number"
                  min={1}
                  max={10000}
                  value={xDailyReadLimit}
                  onChange={e => {
                    const v = parseInt(e.target.value, 10);
                    if (!isNaN(v) && v >= 1) setXDailyReadLimit(v);
                  }}
                  style={{ width: 56, textAlign: 'center', marginLeft: 4, fontSize: '0.78em' }}
                />
                <span style={{ opacity: 0.6, fontSize: '0.78em' }}>条/天</span>
                <span style={{ marginLeft: 10, opacity: 0.5, fontSize: '0.75em' }}>
                  今日已用 {xUsedToday}/{xDailyReadLimit}
                </span>
              </div>
            )}
            <p className="persona-field-hint">
              开启后模型可调用 post_tweet / read_mentions；关闭不注册；随本套人设保存。
            </p>
            <label className="persona-tool-toggle">
              <input
                type="checkbox"
                checked={form.enable_xhs_tool === 1}
                onChange={e =>
                  handleChange('enable_xhs_tool', e.target.checked ? 1 : 0)
                }
              />
              <span>启用小红书工具</span>
            </label>
            {form.enable_xhs_tool === 1 && (
              <div style={{ marginTop: 6, paddingLeft: 34 }}>
                <span style={{ opacity: 0.7, fontSize: '0.78em' }}>读上限</span>
                <input
                  type="number"
                  min={1}
                  max={10000}
                  value={xhsDailyReadLimit}
                  onChange={e => {
                    const v = parseInt(e.target.value, 10);
                    if (!isNaN(v) && v >= 1) setXhsDailyReadLimit(v);
                  }}
                  style={{ width: 56, textAlign: 'center', marginLeft: 4, fontSize: '0.78em' }}
                />
                <span style={{ opacity: 0.6, fontSize: '0.78em', marginLeft: 6 }}>条/天</span>
                <span style={{ marginLeft: 10, opacity: 0.5, fontSize: '0.75em' }}>
                  读已用 {xhsReadUsed}/{xhsDailyReadLimit}
                </span>
                <br />
                <span style={{ opacity: 0.7, fontSize: '0.78em' }}>写上限</span>
                <input
                  type="number"
                  min={1}
                  max={10000}
                  value={xhsDailyWriteLimit}
                  onChange={e => {
                    const v = parseInt(e.target.value, 10);
                    if (!isNaN(v) && v >= 1) setXhsDailyWriteLimit(v);
                  }}
                  style={{ width: 56, textAlign: 'center', marginLeft: 4, fontSize: '0.78em' }}
                />
                <span style={{ opacity: 0.6, fontSize: '0.78em', marginLeft: 6 }}>次/天</span>
                <span style={{ marginLeft: 10, opacity: 0.5, fontSize: '0.75em' }}>
                  写已用 {xhsWriteUsed}/{xhsDailyWriteLimit}
                </span>
              </div>
            )}
            <p className="persona-field-hint">
              开启后可搜索/读笔记/刷推荐/看用户主页/点赞/收藏；需服务器配置 xhs CLI 与 Cookie。链接-only 消息会自动注入正文与配图。
              部署环境须允许（ENABLE_XHS_TOOL，默认开启）；服务端关闭时此处打开也不会注册，且 Telegram 链接预处理不会执行。
            </p>
            <label className="persona-tool-toggle">
              <input
                type="checkbox"
                checked={form.enable_ai_news_tool === 1}
                onChange={e =>
                  handleChange('enable_ai_news_tool', e.target.checked ? 1 : 0)
                }
              />
              <span>启用 AI HOT 资讯工具</span>
            </label>
            <p className="persona-field-hint">
              开启后模型可调用 get_ai_news（条目 / 日报 / 归档）；关闭不注册；随本套人设保存。
              部署环境须允许（ENABLE_AI_NEWS_TOOL，默认开启）；服务端关闭时此处打开也不会注册。
            </p>
          </div>
        </div>

        {/* 右栏：预览区 40% */}
        <div className="persona-preview">
          <div className="preview-header">
            <SectionHead
              slug="[ PROMPT_OUT ]"
              title="拼接预览（与运行时一致）"
              icon={FileCode}
            />
            <button className="copy-btn" onClick={handleCopy} disabled={!clipboardText}>
              复制
            </button>
          </div>
          <div className="preview-content">
            {previewSections.length === 0 ? (
              <p className="preview-empty">
                在左侧填写系统规则 / Char / User 后，此处按{' '}
                <strong>系统规则 → Char → User</strong> 三层展示拼接结果；复制全文仍为一段完整
                plain text，与后端 <code className="persona-inline-code">build_persona_config_system_body</code>{' '}
                输出一致。
              </p>
            ) : (
              <PersonaPreviewStack
                charSections={previewCharSections}
                userChunks={previewUserChunks}
                rulesSection={previewRulesSection}
              />
            )}
          </div>
        </div>
      </div>

      {/* ③ 底部操作栏 */}
      <div className="persona-footer">
        <div className="footer-left">
          <span className="active-name">{activeName}</span>
          {activeId && (
            <>
              <button className="btn-rename" onClick={handleRename}>
                重命名
              </button>
              <button className="btn-danger" onClick={handleDelete}>
                删除
              </button>
            </>
          )}
        </div>
        <div className="footer-right">
          <button
            className={`btn-save ${hasUnsavedChanges ? 'pulse' : ''}`}
            onClick={handleSave}
            disabled={!hasUnsavedChanges || !activeId || isSaving}
          >
            {isSaving ? '保存中...' : '保存'}
          </button>
        </div>
      </div>
      </>
      )}
    </div>
  );
}

export default Persona;
