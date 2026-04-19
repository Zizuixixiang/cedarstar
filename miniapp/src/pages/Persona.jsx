/**
 * 人设配置页面
 * 管理 AI 助手的人设和参数配置
 */
import React, { useState, useEffect, useMemo } from 'react';
import { toast } from 'react-toastify';
import 'react-toastify/dist/ReactToastify.css';
import { Sparkles, UserRound, Wrench, Settings2, FileCode } from 'lucide-react';
import { apiFetch } from '../apiBase';
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
  enable_weather_tool: 0,
  enable_weibo_tool: 0,
  enable_search_tool: 0,
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
  const ca = t(form.char_appearance);
  if (cn) existLines.push(`你的名字是 ${cn}。`);
  if (ci) existLines.push(ci);
  if (ca) existLines.push(ca);
  if (existLines.length > 0) {
    sections.push({
      zone: 'char',
      heading: '【存在定义】',
      body: existLines.join('\n'),
    });
  }

  const cpers = t(form.char_personality);
  if (cpers) {
    sections.push({ zone: 'char', heading: '【内在人格】', body: cpers });
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

  const crels = t(form.char_relationships);
  if (crels) {
    sections.push({ zone: 'char', heading: '【关系与形象】', body: crels });
  }

  const cnsfw = t(form.char_nsfw);
  if (cnsfw) {
    sections.push({ zone: 'char', heading: '【成人内容】', body: cnsfw });
  }

  const toolsParts = [];
  const ctg = t(form.char_tools_guide);
  const com = t(form.char_offline_mode);
  if (ctg) toolsParts.push(`工具使用守则：\n${ctg}`);
  if (com) toolsParts.push(`线下模式：\n${com}`);
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
  const [personas, setPersonas] = useState([]);
  const [activeId, setActiveId] = useState(null);
  const [activeName, setActiveName] = useState('');
  const [form, setForm] = useState(EMPTY_FORM);
  const [savedForm, setSavedForm] = useState(EMPTY_FORM);
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const hasUnsavedChanges = JSON.stringify(form) !== JSON.stringify(savedForm);
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
        enable_weather_tool:
          d.enable_weather_tool != null && Number(d.enable_weather_tool) !== 0
            ? 1
            : 0,
        enable_weibo_tool:
          d.enable_weibo_tool != null && Number(d.enable_weibo_tool) !== 0 ? 1 : 0,
        enable_search_tool:
          d.enable_search_tool != null && Number(d.enable_search_tool) !== 0 ? 1 : 0,
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
      } catch (e) {
        toast.error('加载失败，请刷新重试');
      } finally {
        setIsLoading(false);
      }
    };
    init();
  }, []);

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
      {/* ① 顶部人设切换标签栏 */}
      <div className="persona-tabs">
        <div className="persona-tabs-scroll">
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
                    placeholder="我是谁、本质设定、与世界的关系等（形象在下方「关系与形象」中编辑，预览时并入存在定义块）"
                  />
                </div>
              </PersonaSubBlock>

              <PersonaSubBlock slug="[ INNER ]" title="内在人格">
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

              <PersonaSubBlock slug="[ REL ]" title="关系与形象">
                <div className="field-row">
                  <label className="field-label">机际关系</label>
                  <textarea
                    className="field-textarea"
                    rows={3}
                    value={form.char_relationships}
                    onChange={e => handleChange('char_relationships', e.target.value)}
                    placeholder="与其他角色、用户或实体的关系（预览为「关系与形象」块，仅关系文）"
                  />
                </div>
                <div className="field-row">
                  <label className="field-label">外在形象</label>
                  <textarea
                    className="field-textarea"
                    rows={3}
                    value={form.char_appearance}
                    onChange={e => handleChange('char_appearance', e.target.value)}
                    placeholder="外貌、穿着、视觉特征（预览并入「存在定义」，不重复出现在关系块）"
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
                  <label className="field-label">线下模式</label>
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

          <div className="field-section">
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
    </div>
  );
}

export default Persona;
