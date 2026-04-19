import { BrowserRouter, NavLink, Route, Routes } from 'react-router-dom'
import Home from './pages/Home'
import Diary from './pages/Diary'
import Log from './pages/Log'

function navClass({ isActive }) {
  return isActive ? 'active' : undefined
}

export default function App() {
  return (
    <BrowserRouter basename={import.meta.env.BASE_URL}>
      <div className="app-shell">
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/diary" element={<Diary />} />
          <Route path="/log" element={<Log />} />
        </Routes>
      </div>
      <nav className="bottom-nav">
        <NavLink to="/" end className={navClass}>
          <span className="nav-icon">○</span>
          HOME
        </NavLink>
        <NavLink to="/diary" className={navClass}>
          <span className="nav-icon">≡</span>
          DIARY
        </NavLink>
        <NavLink to="/log" className={navClass}>
          <span className="nav-icon">◷</span>
          LOG
        </NavLink>
      </nav>
    </BrowserRouter>
  )
}
