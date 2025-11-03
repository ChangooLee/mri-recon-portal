import React, { useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../services/auth'
import api from '../services/api'

function Login() {
  const navigate = useNavigate()
  const { fetchUser } = useAuth()

  useEffect(() => {
    // 인증 바이패스: 바로 대시보드로 이동
    const autoLogin = async () => {
      try {
        const response = await api.get('/auth/me')
        if (response.data) {
          localStorage.setItem('auth_token', 'bypass-token')
          await fetchUser()
          navigate('/dashboard', { replace: true })
        }
      } catch (error) {
        console.error('Auto login failed:', error)
      }
    }
    
    autoLogin()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div style={{
      display: 'flex',
      justifyContent: 'center',
      alignItems: 'center',
      minHeight: '100vh',
      background: 'linear-gradient(135deg, var(--primary-color) 0%, var(--primary-dark) 100%)',
      position: 'relative',
      overflow: 'hidden'
    }}>
      {/* 배경 장식 요소 */}
      <div style={{
        position: 'absolute',
        width: '500px',
        height: '500px',
        borderRadius: '50%',
        background: 'rgba(255, 255, 255, 0.1)',
        top: '-200px',
        right: '-200px',
        filter: 'blur(60px)'
      }} />
      <div style={{
        position: 'absolute',
        width: '400px',
        height: '400px',
        borderRadius: '50%',
        background: 'rgba(255, 255, 255, 0.1)',
        bottom: '-150px',
        left: '-150px',
        filter: 'blur(60px)'
      }} />
      
      <div style={{
        background: 'rgba(255, 255, 255, 0.98)',
        padding: '60px 50px',
        borderRadius: '20px',
        boxShadow: '0 20px 60px rgba(0,0,0,0.3)',
        textAlign: 'center',
        maxWidth: '450px',
        width: '90%',
        position: 'relative',
        zIndex: 1,
        backdropFilter: 'blur(10px)'
      }}>
        <div style={{
          fontSize: '32px',
          fontWeight: '700',
          background: 'linear-gradient(135deg, var(--primary-color) 0%, var(--primary-dark) 100%)',
          WebkitBackgroundClip: 'text',
          WebkitTextFillColor: 'transparent',
          marginBottom: '12px'
        }}>
          MRI 3D Reconstruction
        </div>
        <p style={{
          marginBottom: '40px',
          color: 'var(--text-secondary)',
          fontSize: '16px',
          lineHeight: '1.6'
        }}>
          의료용 MRI DICOM 데이터를 업로드하고<br />3D 모델로 재구성하세요
        </p>
        <div style={{
          padding: '14px 32px',
          fontSize: '16px',
          fontWeight: '600',
          background: 'var(--primary-color)',
          color: 'white',
          borderRadius: '12px',
          display: 'inline-flex',
          alignItems: 'center',
          gap: '12px',
          boxShadow: '0 4px 15px rgba(102, 126, 234, 0.4)'
        }}>
          <svg width="20" height="20" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
          </svg>
          로그인 중...
        </div>
        
        <div style={{
          marginTop: '30px',
          paddingTop: '30px',
          borderTop: '1px solid var(--border-color)',
          fontSize: '14px',
          color: 'var(--text-secondary)'
        }}>
          <p style={{ marginBottom: '8px' }}>의료 전문성과 혁신적인 기술로</p>
          <p>더 빠르고 정확한 3D 재구성을 제공합니다</p>
        </div>
      </div>
    </div>
  )
}

export default Login

