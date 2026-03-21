/**
 * 路由配置文件
 * 定义所有页面路由
 */
import React from 'react'
import Dashboard from './pages/Dashboard.jsx'
import Persona from './pages/Persona.jsx'
import Memory from './pages/Memory.jsx'
import History from './pages/History.jsx'
import Logs from './pages/Logs.jsx'
import Config from './pages/Config.jsx'
import Settings from './pages/Settings.jsx'

/**
 * 导航菜单配置
 * 包含图标、文字和路由路径
 */
export const navItems = [
  { icon: '✨', text: '控制台概览', path: '/' },
  { icon: '🪐', text: '人设与参数', path: '/persona' },
  { icon: '📓', text: '记忆日记本', path: '/memory' },
  { icon: '🕰️', text: '时光机历史', path: '/history' },
  { icon: '🪵', text: '系统日志', path: '/logs' },
  { icon: '⚙️', text: '助手配置', path: '/config' },
  { icon: '🔑', text: '核心设置', path: '/settings' },
]

/**
 * 路由配置
 */
export const routes = [
  { path: '/', element: <Dashboard /> },
  { path: '/persona', element: <Persona /> },
  { path: '/memory', element: <Memory /> },
  { path: '/history', element: <History /> },
  { path: '/logs', element: <Logs /> },
  { path: '/config', element: <Config /> },
  { path: '/settings', element: <Settings /> },
]
