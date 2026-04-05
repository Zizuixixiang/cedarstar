/**
 * 根组件
 * 包含侧边栏和路由出口，管理侧边栏展开/收起状态
 */
import { useState } from 'react'
import { BrowserRouter, Routes, Route, NavLink, useLocation } from 'react-router-dom'
import { navItems, routes } from './router.jsx'
import './styles/sidebar.css'

/**
 * 侧边栏组件
 * @param {boolean} collapsed - 是否收起状态
 * @param {function} onToggle - 切换状态的回调函数
 * @param {boolean} mobileOpen - 移动端是否展开
 * @param {function} onMobileClose - 移动端关闭回调
 */
function Sidebar({ collapsed, onToggle, mobileOpen, onMobileClose }) {
  return (
    <>
      {/* 移动端全局遮罩层 */}
      <div 
        className={`sidebar-overlay ${mobileOpen ? 'active' : ''}`} 
        onClick={onMobileClose}
      ></div>

      <aside className={`sidebar ${collapsed ? 'collapsed' : ''} ${mobileOpen ? 'mobile-open' : ''}`}>
        {/* 侧边栏头部 */}
        <div className="sidebar-header">
          <span className="sidebar-logo">✦ Sirius Core</span>
          <button className="sidebar-toggle desktop-only" onClick={onToggle} aria-label="切换侧边栏">
            ☰
          </button>
        </div>

        {/* 导航菜单 */}
        <nav className="sidebar-nav">
          {navItems.map((item) => (
            <NavLink
              key={item.path}
              to={item.path}
              className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`}
              onClick={onMobileClose}
            >
              <span className="nav-icon">{item.icon}</span>
              <span className="nav-text">{item.text}</span>
            </NavLink>
          ))}
        </nav>
      </aside>
    </>
  )
}

/**
 * 主内容区组件
 * @param {boolean} sidebarCollapsed - 侧边栏是否收起
 * @param {function} onOpenMobileSidebar - 移动端打开侧边栏回调
 */
function MainContent({ sidebarCollapsed, onOpenMobileSidebar }) {
  return (
    <main className={`main-content ${sidebarCollapsed ? 'sidebar-collapsed' : ''}`}>
      {/* 移动端 Header 区域 */}
      <div className="mobile-header">
        <button className="mobile-menu-btn" onClick={onOpenMobileSidebar} aria-label="打开菜单">
          ☰
        </button>
        <span className="mobile-logo">✦ Sirius Core</span>
      </div>
      
      <div className="main-content-inner">
        <div className="main-content-viewport">
          <Routes>
            {routes.map((route) => (
              <Route key={route.path} path={route.path} element={route.element} />
            ))}
          </Routes>
        </div>
      </div>
    </main>
  )
}

/** 与 vite.config.js 的 base 对齐；生产环境挂在 /app，无 basename 时路径 /app/ 无法匹配路由 "/" */
function routerBasename() {
  const raw = import.meta.env.BASE_URL || '/'
  const trimmed = raw.replace(/\/$/, '')
  return trimmed === '' ? undefined : trimmed
}

/**
 * 应用根组件
 */
function App() {
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false)
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false)

  /**
   * 切换侧边栏展开/收起状态 (桌面端)
   */
  const handleToggleSidebar = () => {
    setSidebarCollapsed(!sidebarCollapsed)
  }

  return (
    <BrowserRouter basename={routerBasename()}>
      <div className="app-container">
        <Sidebar 
          collapsed={sidebarCollapsed} 
          onToggle={handleToggleSidebar}
          mobileOpen={mobileSidebarOpen}
          onMobileClose={() => setMobileSidebarOpen(false)}
        />
        <MainContent 
          sidebarCollapsed={sidebarCollapsed} 
          onOpenMobileSidebar={() => setMobileSidebarOpen(true)}
        />
      </div>
    </BrowserRouter>
  )
}

export default App
