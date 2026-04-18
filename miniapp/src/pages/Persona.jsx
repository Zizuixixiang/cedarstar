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
  char_personality: '',
  char_speech_style: '',
  char_appearance: '',
  char_relationships: '',
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
};

// 本地拼接 System Prompt 预览
function buildPreview(form) {
  const parts = [];
  
  // Char 人设部分
    if (form.char_name?.trim() || form.char_personality?.trim() || form.char_speech_style?.trim() || form.char_appearance?.trim() || form.char_relationships?.trim()) {
    const charParts = [];
    if (form.char_name?.trim()) charParts.push(`姓名：${form.char_name.trim()}`);
    if (form.char_personality?.trim()) charParts.push(`性格：${form.char_personality.trim()}`);
    if (form.char_speech_style?.trim()) charParts.push(`说话方式：${form.char_speech_style.trim()}`);
    if (form.char_appearance?.trim()) charParts.push(`形象：${form.char_appearance.trim()}`);
    if (form.char_relationships?.trim()) charParts.push(`机际关系：${form.char_relationships.trim()}`);
    if (charParts.length > 0) parts.push(`【Char 人设】\n${charParts.join('\n')}`);
  }
  
  // 我的人设部分
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
    parts.push(`【User 的人设】\n${userParts.join('\n')}`);
  }
  
  if (form.system_rules?.trim())
    parts.push(`【系统规则】\n${form.system_rules.trim()}`);
  return parts.join('\n\n');
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
          {[6, 3, 3, 3, 3, 4].map((rows, i) => (
            <div key={i} className="field-section">
              <div className="sk-block sk-title" />
              <div className="sk-block sk-textarea" style={{ height: rows * 22 + 24 }} />
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
  const [nsfwExpanded, setNsfwExpanded] = useState(false);

  const hasUnsavedChanges = JSON.stringify(form) !== JSON.stringify(savedForm);
  const preview = useMemo(() => buildPreview(form), [form]);

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
        char_personality: d.char_personality || '',
        char_speech_style: d.char_speech_style || '',
        char_appearance: d.char_appearance || '',
        char_relationships: d.char_relationships || '',
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
    if (!preview) return;
    navigator.clipboard.writeText(preview).then(() => {
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

          {/* Char 的人设 */}
          <div className="field-section">
            <SectionHead slug="[ CHAR_PERSONA ]" title="Char 的人设" icon={Sparkles} />
            
            <div className="field-row">
              <label className="field-label">姓名</label>
              <input
                type="text"
                className="field-input"
                value={form.char_name}
                onChange={e => handleChange('char_name', e.target.value)}
                placeholder="Char的姓名或称呼"
              />
            </div>

            <div className="field-row">
              <label className="field-label">性格</label>
              <textarea
                className="field-textarea"
                rows={3}
                value={form.char_personality}
                onChange={e => handleChange('char_personality', e.target.value)}
                placeholder="Char的性格特征、心理特点等"
              />
            </div>

            <div className="field-row">
              <label className="field-label">说话方式</label>
              <textarea
                className="field-textarea"
                rows={3}
                value={form.char_speech_style}
                onChange={e => handleChange('char_speech_style', e.target.value)}
                placeholder="Char的口头禅、语气、措辞风格等"
              />
            </div>

            <div className="field-row">
              <label className="field-label">形象</label>
              <textarea
                className="field-textarea"
                rows={3}
                value={form.char_appearance}
                onChange={e => handleChange('char_appearance', e.target.value)}
                placeholder="Char的形象特征、外貌、穿着等"
              />
            </div>

            <div className="field-row">
              <label className="field-label">机际关系</label>
              <textarea
                className="field-textarea"
                rows={3}
                value={form.char_relationships}
                onChange={e => handleChange('char_relationships', e.target.value)}
                placeholder="Char与其他AI或实体的机际关系"
              />
            </div>
          </div>

          {/* User 的人设 */}
          <div className="field-section">
            <SectionHead slug="[ USER_PERSONA ]" title="User 的人设" icon={UserRound} />

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

            <div className="field-row">
              <label className="field-label">禁忌</label>
              <textarea
                className="field-textarea"
                rows={2}
                value={form.user_taboos}
                onChange={e => handleChange('user_taboos', e.target.value)}
                placeholder="聊天中的禁区"
              />
            </div>

            {/* NSFW 折叠区 */}
            <div className="nsfw-section">
              <button
                className="nsfw-toggle"
                onClick={() => setNsfwExpanded(v => !v)}
              >
                <span className={`nsfw-arrow ${nsfwExpanded ? 'up' : ''}`}>▶</span>
                {nsfwExpanded ? '收起 NSFW 设置' : '展开 NSFW 设置'}
              </button>
              <div className={`nsfw-content ${nsfwExpanded ? 'expanded' : ''}`}>
                <div className="field-row" style={{ marginTop: 8 }}>
                  <label className="field-label">NSFW 偏好</label>
                  <textarea
                    className="field-textarea"
                    rows={2}
                    value={form.user_nsfw}
                    onChange={e => handleChange('user_nsfw', e.target.value)}
                  />
                </div>
              </div>
            </div>

            <div className="field-row">
              <label className="field-label">其他</label>
              <textarea
                className="field-textarea"
                rows={2}
                value={form.user_other}
                onChange={e => handleChange('user_other', e.target.value)}
                placeholder="其他需要注意的事项"
              />
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
          </div>

          {/* 系统规则 */}
          <div className="field-section">
            <SectionHead slug="[ CORE_RULES ]" title="系统规则" icon={Settings2} />
            <p className="persona-field-hint">
              Telegram 以 HTML 发正文；请用 HTML 标签排版（如
              <code className="persona-inline-code">&lt;b&gt;</code>
              、
              <code className="persona-inline-code">&lt;code&gt;</code>
              、
              <code className="persona-inline-code">&lt;i&gt;</code>
              ），勿用 Markdown。
            </p>
            <textarea
              className="field-textarea"
              rows={4}
              value={form.system_rules}
              onChange={e => handleChange('system_rules', e.target.value)}
              placeholder="记忆指令、格式要求、行为约束..."
            />
          </div>
        </div>

        {/* 右栏：预览区 40% */}
        <div className="persona-preview">
          <div className="preview-header">
            <SectionHead slug="[ PROMPT_PREVIEW ]" title="System Prompt 预览" icon={FileCode} />
            <button className="copy-btn" onClick={handleCopy} disabled={!preview}>
              复制全文
            </button>
          </div>
          <pre className="preview-content">
            {preview || '（填写左侧内容后将在此实时预览）'}
          </pre>
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
