import React from 'react';
import { Navigate } from 'react-router-dom';
import {
  LayoutDashboard,
  UserCircle,
  BookMarked,
  History as HistoryIcon,
  ScrollText,
  SlidersHorizontal,
  KeyRound,
  Activity,
  ClipboardCheck,
  Wrench,
} from 'lucide-react';
import Dashboard from './pages/Dashboard.jsx';
import Persona from './pages/Persona.jsx';
import Memory from './pages/Memory.jsx';
import History from './pages/History.jsx';
import Logs from './pages/Logs.jsx';
import Config from './pages/Config.jsx';
import Settings from './pages/Settings.jsx';
import Observability from './pages/Observability.jsx';
import Approvals from './pages/Approvals.jsx';
import ToolsCenter from './pages/ToolsCenter.jsx';

export const navItems = [
  { Icon: LayoutDashboard, text: '控制台概览', path: '/' },
  { Icon: UserCircle, text: '人设与参数', path: '/persona' },
  { Icon: BookMarked, text: '记忆日记本', path: '/memory' },
  { Icon: ClipboardCheck, text: '待审批', path: '/approvals' },
  { Icon: HistoryIcon, text: '时光机历史', path: '/history' },
  { Icon: ScrollText, text: '系统日志', path: '/logs' },
  { Icon: Wrench, text: '工具中心', path: '/tools' },
  { Icon: SlidersHorizontal, text: '助手配置', path: '/config', dividerBefore: true },
  { Icon: KeyRound, text: '核心设置', path: '/settings' },
  { Icon: Activity, text: '调用观测', path: '/observability' },
];

export const routes = [
  { path: '/', element: <Dashboard /> },
  { path: '/persona', element: <Persona /> },
  { path: '/memory', element: <Memory /> },
  { path: '/approvals', element: <Approvals /> },
  { path: '/history', element: <History /> },
  { path: '/logs', element: <Logs /> },
  { path: '/tools', element: <ToolsCenter /> },
  { path: '/config', element: <Config /> },
  { path: '/settings', element: <Settings /> },
  { path: '/observability', element: <Observability /> },
  { path: '*', element: <Navigate to="/" replace /> },
];
