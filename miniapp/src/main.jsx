/**
 * 应用入口文件
 * 初始化 React 应用并挂载到 DOM
 */
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App.jsx'
import { APP_DISPLAY_NAME } from './appName.js'
import './styles/global.css'

document.title = APP_DISPLAY_NAME

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
