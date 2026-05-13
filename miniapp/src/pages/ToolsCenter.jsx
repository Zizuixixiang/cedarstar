import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { RefreshCw, Wrench, TrendingUp } from 'lucide-react';
import { apiFetch } from '../apiBase';
import '../styles/tools-center.css';

const TOOL_SWITCH_KEYS = [
  { key: 'enable_lutopia', label: 'Lutopia 论坛工具' },
  { key: 'enable_rcommunity', label: 'rcommunity 论坛 MCP' },
  { key: 'enable_weather_tool', label: '天气工具' },
  { key: 'enable_weibo_tool', label: '微博热搜工具' },
  { key: 'enable_search_tool', label: '网页搜索工具' },
  { key: 'enable_x_tool', label: 'X (Twitter) 工具' },
  { key: 'enable_xhs_tool', label: '小红书工具' },
  { key: 'enable_ai_news_tool', label: 'AI HOT 资讯工具' },
];

const SETTINGS_CONFIG_TYPES = [
  { key: 'search_summary', label: '搜索摘要模型' },
  { key: 'tts', label: 'TTS 语音合成' },
  { key: 'stt', label: 'STT 语音转录' },
];

function StatusBadge({ enabled }) {
  return (
    <span className={`tools-status ${enabled ? 'on' : 'off'}`}>
      {enabled ? '已启用' : '已关闭'}
    </span>
  );
}

export default function ToolsCenter() {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [personaName, setPersonaName] = useState('');
  const [toolSwitches, setToolSwitches] = useState({});
  const [xDailyReadLimit, setXDailyReadLimit] = useState(100);
  const [xUsage, setXUsage] = useState({ used_today: 0 });
  const [xhsReadLimit, setXhsReadLimit] = useState(80);
  const [xhsWriteLimit, setXhsWriteLimit] = useState(30);
  const [xhsUsage, setXhsUsage] = useState({ read_used: 0, write_used: 0 });
  const [usageLoading, setUsageLoading] = useState(false);
  const [apiConfigs, setApiConfigs] = useState({
    search_summary: [],
    tts: [],
    stt: [],
  });
  const [toolExecutions, setToolExecutions] = useState([]);

  const loadXUsage = useCallback(async () => {
    setUsageLoading(true);
    try {
      const [usageRes, configRes, xhsUsageRes] = await Promise.all([
        apiFetch('/api/config/x-usage'),
        apiFetch('/api/config/config'),
        apiFetch('/api/config/xhs-usage'),
      ]);
      const usageData = await usageRes.json();
      const configData = await configRes.json();
      const xhsUsageData = await xhsUsageRes.json();
      if (usageData.success && usageData.data) {
        setXUsage(usageData.data);
      }
      if (configData.success && configData.data?.x_daily_read_limit != null) {
        setXDailyReadLimit(Number(configData.data.x_daily_read_limit) || 100);
      }
      if (configData.success && configData.data?.xhs_daily_read_limit != null) {
        setXhsReadLimit(Number(configData.data.xhs_daily_read_limit) || 80);
      }
      if (configData.success && configData.data?.xhs_daily_write_limit != null) {
        setXhsWriteLimit(Number(configData.data.xhs_daily_write_limit) || 30);
      }
      if (xhsUsageData?.success && xhsUsageData?.data) {
        setXhsUsage(xhsUsageData.data);
      }
    } finally {
      setUsageLoading(false);
    }
  }, []);

  const loadAll = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const personaListRes = await apiFetch('/api/persona');
      const personaListData = await personaListRes.json();
      const personas = personaListData?.success ? personaListData.data || [] : [];
      const firstPersonaId = personas[0]?.id;
      const detailPromise = firstPersonaId
        ? apiFetch(`/api/persona/${firstPersonaId}`).then((r) => r.json())
        : Promise.resolve(null);

      const [
        detailData,
        configData,
        xUsageData,
        xhsUsageData,
        searchSummaryData,
        ttsData,
        sttData,
        toolExecData,
      ] = await Promise.all([
        detailPromise,
        apiFetch('/api/config/config').then((r) => r.json()),
        apiFetch('/api/config/x-usage').then((r) => r.json()),
        apiFetch('/api/config/xhs-usage').then((r) => r.json()),
        apiFetch('/api/settings/api-configs?config_type=search_summary').then((r) =>
          r.json()
        ),
        apiFetch('/api/settings/api-configs?config_type=tts').then((r) => r.json()),
        apiFetch('/api/settings/api-configs?config_type=stt').then((r) => r.json()),
        apiFetch('/api/observability/tool-executions?limit=8').then((r) => r.json()),
      ]);

      if (detailData?.success && detailData?.data) {
        const d = detailData.data;
        setPersonaName(d.name || personas[0]?.name || '默认人设');
        setToolSwitches({
          enable_lutopia: Number(d.enable_lutopia || 0),
          enable_rcommunity: Number(d.enable_rcommunity || 0),
          enable_weather_tool: Number(d.enable_weather_tool || 0),
          enable_weibo_tool: Number(d.enable_weibo_tool || 0),
          enable_search_tool: Number(d.enable_search_tool || 0),
          enable_x_tool: Number(d.enable_x_tool || 0),
          enable_xhs_tool: Number(d.enable_xhs_tool || 0),
          enable_ai_news_tool: Number(d.enable_ai_news_tool || 0),
        });
      } else {
        setPersonaName(personas[0]?.name || '未找到人设');
      }

      if (configData?.success && configData?.data?.x_daily_read_limit != null) {
        setXDailyReadLimit(Number(configData.data.x_daily_read_limit) || 100);
      }
      if (configData?.success && configData?.data?.xhs_daily_read_limit != null) {
        setXhsReadLimit(Number(configData.data.xhs_daily_read_limit) || 80);
      }
      if (configData?.success && configData?.data?.xhs_daily_write_limit != null) {
        setXhsWriteLimit(Number(configData.data.xhs_daily_write_limit) || 30);
      }
      if (xUsageData?.success && xUsageData?.data) {
        setXUsage(xUsageData.data);
      }
      if (xhsUsageData?.success && xhsUsageData?.data) {
        setXhsUsage(xhsUsageData.data);
      }

      setApiConfigs({
        search_summary: searchSummaryData?.success ? searchSummaryData.data || [] : [],
        tts: ttsData?.success ? ttsData.data || [] : [],
        stt: sttData?.success ? sttData.data || [] : [],
      });

      const toolRows = toolExecData?.success
        ? Array.isArray(toolExecData.data)
          ? toolExecData.data
          : toolExecData.data?.items || []
        : [];
      setToolExecutions(toolRows);
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  const activeConfigMap = useMemo(() => {
    const out = {};
    for (const item of SETTINGS_CONFIG_TYPES) {
      const list = apiConfigs[item.key] || [];
      out[item.key] = list.find((cfg) => Number(cfg.is_active || 0) === 1) || null;
    }
    return out;
  }, [apiConfigs]);

  return (
    <div className="tools-page">
      <header className="tools-header">
        <div className="tools-title-wrap">
          <p className="tools-kicker">TOOLS CENTER</p>
          <h1>工具中心</h1>
        </div>
        <button type="button" className="tools-refresh-btn" onClick={loadAll}>
          <RefreshCw size={14} aria-hidden />
          刷新全部
        </button>
      </header>

      {error ? <div className="tools-error">加载失败：{error}</div> : null}
      {loading ? <div className="tools-loading">加载中...</div> : null}

      {!loading && (
        <>
          <section className="tools-card">
            <div className="tools-card-head">
              <div className="tools-card-title">
                <Wrench size={16} aria-hidden />
                工具可用性
              </div>
              <Link className="tools-link-btn" to="/persona?section=tools">
                去人设页编辑
              </Link>
            </div>
            <p className="tools-muted">当前展示人设：{personaName || '未命名'}</p>
            <p className="tools-muted" style={{ marginTop: 8, lineHeight: 1.5 }}>
              下表为只读快照。要<strong>开启/关闭</strong>各工具（含<strong>小红书</strong>、X、天气等），请到{' '}
              <Link className="tools-link-btn" to="/persona?section=tools" style={{ display: 'inline', padding: '0 0.2em' }}>
                人设与参数
              </Link>
              ，在左侧栏向下滚动到 <strong>「[ SYS_TOOLS ] 工具」</strong> 区块勾选后<strong>保存人设</strong>。
            </p>
            <div className="tools-switch-list">
              {TOOL_SWITCH_KEYS.map((item) => (
                <div className="tools-switch-row" key={item.key}>
                  <span>{item.label}</span>
                  <StatusBadge enabled={Number(toolSwitches[item.key] || 0) === 1} />
                </div>
              ))}
            </div>
          </section>

          <section className="tools-card">
            <div className="tools-card-head">
              <div className="tools-card-title">
                <TrendingUp size={16} aria-hidden />
                X 配额概览
              </div>
              <button
                type="button"
                className="tools-link-btn ghost"
                onClick={loadXUsage}
                disabled={usageLoading}
              >
                {usageLoading ? '刷新中...' : '刷新配额'}
              </button>
            </div>
            <div className="tools-usage-line">
              今日已用 <strong>{Number(xUsage.used_today || 0)}</strong> /{' '}
              <strong>{xDailyReadLimit}</strong>
            </div>
          </section>

          <section className="tools-card">
            <div className="tools-card-head">
              <div className="tools-card-title">
                <TrendingUp size={16} aria-hidden />
                小红书配额概览
              </div>
              <button
                type="button"
                className="tools-link-btn ghost"
                onClick={loadXUsage}
                disabled={usageLoading}
              >
                {usageLoading ? '刷新中...' : '刷新配额'}
              </button>
            </div>
            <div className="tools-usage-line">
              读： <strong>{Number(xhsUsage.read_used || 0)}</strong> / <strong>{xhsReadLimit}</strong>
              {' · '}
              写： <strong>{Number(xhsUsage.write_used || 0)}</strong> / <strong>{xhsWriteLimit}</strong>
            </div>
          </section>

          <section className="tools-card">
            <div className="tools-card-head">
              <div className="tools-card-title">工具模型配置（只读）</div>
              <Link className="tools-link-btn" to="/settings?section=api-configs">
                去设置页编辑
              </Link>
            </div>
            <div className="tools-config-list">
              {SETTINGS_CONFIG_TYPES.map((item) => {
                const active = activeConfigMap[item.key];
                return (
                  <div className="tools-config-row" key={item.key}>
                    <span className="tools-config-label">{item.label}</span>
                    <span className="tools-config-value">
                      {active ? active.model || active.name || '已配置' : '未配置'}
                    </span>
                  </div>
                );
              })}
            </div>
          </section>

          <section className="tools-card">
            <div className="tools-card-head">
              <div className="tools-card-title">最近工具执行（只读）</div>
              <Link className="tools-link-btn" to="/observability">
                查看全部观测
              </Link>
            </div>
            <div className="tools-exec-list">
              {toolExecutions.length === 0 ? (
                <div className="tools-muted">暂无工具执行记录</div>
              ) : (
                toolExecutions.map((row) => (
                  <div className="tools-exec-row" key={row.id}>
                    <span className="name">{row.tool_name || 'unknown'}</span>
                    <span className="meta">
                      {row.platform || 'unknown'} · turn {row.turn_id}
                    </span>
                  </div>
                ))
              )}
            </div>
          </section>
        </>
      )}
    </div>
  );
}
