import React, { useEffect } from 'react'
import { BrowserRouter as Router, Routes, Route, Navigate, useNavigate } from 'react-router-dom'
import { AuthProvider, useAuth } from './services/auth'
import Login from './pages/Login'
import Dashboard from './pages/Dashboard'
import Viewer from './pages/Viewer'
import MeshViewer from './pages/MeshViewer'
import api from './services/api'

// 자동 로그인 처리 컴포넌트
function AutoLoginRedirect() {
  const navigate = useNavigate()
  const { user, loading } = useAuth()

  useEffect(() => {
    if (!loading) {
      if (user) {
        navigate('/dashboard', { replace: true })
      } else {
        // 로그인 페이지로 이동 (자동 로그인 처리)
        navigate('/login', { replace: true })
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user, loading])

  return (
    <div style={{ 
      padding: '20px', 
      textAlign: 'center',
      minHeight: '100vh',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center'
    }}>
      <div>로딩 중...</div>
    </div>
  )
}

function App() {
  return (
    <AuthProvider>
      <Router>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route path="/auth/callback" element={<Login />} />
          <Route path="/auth/google/callback" element={<Login />} />
          <Route path="/dashboard" element={<Dashboard />} />
          <Route path="/viewer/:reconstructionId" element={<Viewer />} />
          <Route path="/mesh/:reconstructionId" element={<MeshViewer />} />
          <Route path="/" element={<AutoLoginRedirect />} />
        </Routes>
      </Router>
    </AuthProvider>
  )
}

export default App

