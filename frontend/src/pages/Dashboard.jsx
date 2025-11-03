import React, { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../services/auth'
import api from '../services/api'

function Dashboard() {
  const { user, logout, loading: authLoading } = useAuth()
  const navigate = useNavigate()
  const [reconstructions, setReconstructions] = useState([])
  const [loading, setLoading] = useState(true)
  const [uploading, setUploading] = useState(false)

  useEffect(() => {
    // 로딩 중이면 대기
    if (authLoading) {
      return
    }
    
    // 사용자가 없으면 로그인 페이지로 이동
    if (!user) {
      navigate('/login', { replace: true })
      return
    }
    
    // 사용자가 있으면 데이터 로드
    fetchReconstructions()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user, authLoading])

  const fetchReconstructions = async () => {
    try {
      const response = await api.get('/reconstruct')
      setReconstructions(response.data)
    } catch (error) {
      console.error('Failed to fetch reconstructions:', error)
    } finally {
      setLoading(false)
    }
  }

  // 자동 새로고침 (30초마다)
  useEffect(() => {
    if (!user) return
    
    const interval = setInterval(() => {
      fetchReconstructions()
    }, 30000) // 30초마다
    
    return () => clearInterval(interval)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user])

  const handleFileUpload = async (event) => {
    const files = Array.from(event.target.files)
    if (files.length === 0) return

    // 파일명으로 정렬 (DICOM 시리즈는 보통 파일명 순서가 중요)
    files.sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' }))

    setUploading(true)
    const formData = new FormData()
    files.forEach((file) => {
      formData.append('files', file)
    })

    try {
      const fileCount = files.length
      console.log(`Uploading ${fileCount} DICOM file(s)...`)
      
      await api.post('/reconstruct/upload', formData, {
        headers: {
          'Content-Type': 'multipart/form-data',
        },
      })
      
      alert(`업로드 완료! ${fileCount}개의 DICOM 파일이 처리되었습니다.`)
      
      // 재구성 목록 새로고침
      setTimeout(() => {
        fetchReconstructions()
      }, 2000)
    } catch (error) {
      console.error('Upload failed:', error)
      alert(`업로드 실패: ${error.response?.data?.detail || error.message || 'Unknown error'}`)
    } finally {
      setUploading(false)
      // 파일 input 초기화
      event.target.value = ''
    }
  }

  const getStatusColor = (status) => {
    switch (status) {
      case 'completed':
        return '#4caf50'
      case 'processing':
        return '#ff9800'
      case 'failed':
        return '#f44336'
      default:
        return '#9e9e9e'
    }
  }

  if (authLoading || loading) {
    return <div style={{ padding: '20px' }}>Loading...</div>
  }

  return (
    <div style={{ minHeight: '100vh', background: 'var(--bg-light)' }}>
      <header style={{
        background: 'var(--bg-white)',
        padding: '20px 40px',
        boxShadow: 'var(--shadow)',
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        position: 'sticky',
        top: 0,
        zIndex: 100
      }}>
        <div style={{
          fontSize: '24px',
          fontWeight: '700',
          background: 'linear-gradient(135deg, var(--primary-color) 0%, var(--primary-dark) 100%)',
          WebkitBackgroundClip: 'text',
          WebkitTextFillColor: 'transparent'
        }}>
          MRI 3D Reconstruction
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '20px' }}>
          {user && (
            <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
              {user.avatar_url && (
                <img
                  src={user.avatar_url}
                  alt={user.name}
                  style={{
                    width: '36px',
                    height: '36px',
                    borderRadius: '50%',
                    objectFit: 'cover'
                  }}
                />
              )}
              <span style={{ color: 'var(--text-primary)', fontWeight: '500' }}>
                {user.name || user.email}
              </span>
            </div>
          )}
          <button onClick={logout} style={{
            padding: '10px 20px',
            background: 'var(--text-secondary)',
            color: 'white',
            border: 'none',
            borderRadius: '8px',
            cursor: 'pointer',
            fontWeight: '500',
            transition: 'all 0.3s ease'
          }}>
            로그아웃
          </button>
        </div>
      </header>

      <main style={{ maxWidth: '1200px', margin: '0 auto', padding: '40px 20px' }}>
        <div style={{
          background: 'var(--bg-white)',
          padding: '40px',
          borderRadius: '16px',
          marginBottom: '30px',
          boxShadow: 'var(--shadow)',
          border: '1px solid var(--border-color)'
        }}>
          <h2 style={{
            marginBottom: '12px',
            fontSize: '24px',
            fontWeight: '700',
            color: 'var(--text-primary)'
          }}>
            DICOM 파일 업로드
          </h2>
          <p style={{
            marginBottom: '12px',
            color: 'var(--text-primary)',
            fontSize: '15px',
            fontWeight: '500'
          }}>
            여러 DICOM 파일을 한 번에 선택하세요
          </p>
          <p style={{
            marginBottom: '30px',
            color: 'var(--text-secondary)',
            fontSize: '14px',
            lineHeight: '1.6'
          }}>
            💡 DICOM 시리즈(여러 슬라이스)를 모두 선택하면 3D 재구성의 정확도가 향상됩니다.<br />
            예: 119개의 DICOM 파일을 모두 선택하여 업로드하세요
          </p>
          <label style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: '10px',
            padding: '14px 28px',
            background: uploading ? '#ccc' : 'linear-gradient(135deg, var(--primary-color) 0%, var(--primary-dark) 100%)',
            color: 'white',
            borderRadius: '12px',
            cursor: uploading ? 'not-allowed' : 'pointer',
            fontWeight: '600',
            fontSize: '16px',
            boxShadow: uploading ? 'none' : '0 4px 15px rgba(102, 126, 234, 0.4)',
            transition: 'all 0.3s ease'
          }}>
            {uploading ? (
              <>
                <svg width="20" height="20" fill="none" stroke="currentColor" viewBox="0 0 24 24" style={{ animation: 'spin 1s linear infinite' }}>
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                </svg>
                <span>업로드 중...</span>
              </>
            ) : (
              <>
                <svg width="20" height="20" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12" />
                </svg>
                <span>DICOM 파일 선택 (여러 파일 가능)</span>
              </>
            )}
            <input
              type="file"
              multiple
              accept=".dcm,.dicom"
              onChange={handleFileUpload}
              disabled={uploading}
              style={{ display: 'none' }}
            />
          </label>
        </div>

        <div style={{
          background: 'var(--bg-white)',
          padding: '40px',
          borderRadius: '16px',
          boxShadow: 'var(--shadow)',
          border: '1px solid var(--border-color)'
        }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '30px' }}>
            <h2 style={{
              margin: 0,
              fontSize: '24px',
              fontWeight: '700',
              color: 'var(--text-primary)'
            }}>
              재구성 작업 이력
            </h2>
            <div style={{
              display: 'flex',
              gap: '12px',
              alignItems: 'center'
            }}>
              <div style={{
                fontSize: '14px',
                color: 'var(--text-secondary)',
                padding: '6px 12px',
                background: 'var(--bg-light)',
                borderRadius: '6px'
              }}>
                총 {reconstructions.length}개
              </div>
              <button
                onClick={fetchReconstructions}
                style={{
                  padding: '8px 16px',
                  background: 'var(--primary-color)',
                  color: 'white',
                  border: 'none',
                  borderRadius: '8px',
                  cursor: 'pointer',
                  fontSize: '13px',
                  fontWeight: '500',
                  transition: 'all 0.3s ease'
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.background = 'var(--primary-dark)'
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.background = 'var(--primary-color)'
                }}
              >
                새로고침
              </button>
            </div>
          </div>
          {reconstructions.length === 0 ? (
            <div style={{
              textAlign: 'center',
              padding: '60px 20px',
              color: 'var(--text-secondary)'
            }}>
              <svg width="64" height="64" fill="none" stroke="currentColor" viewBox="0 0 24 24" style={{ margin: '0 auto 20px', opacity: 0.5 }}>
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
              </svg>
              <p style={{ fontSize: '16px', marginBottom: '8px' }}>아직 재구성 작업이 없습니다</p>
              <p style={{ fontSize: '14px' }}>DICOM 파일을 업로드하여 시작하세요</p>
            </div>
          ) : (
            <div style={{ display: 'grid', gap: '16px' }}>
              {reconstructions.map((recon) => (
                <div
                  key={recon.id}
                  style={{
                    padding: '24px',
                    borderRadius: '12px',
                    border: '1px solid var(--border-color)',
                    background: recon.status === 'completed' ? 'linear-gradient(135deg, rgba(102, 126, 234, 0.05) 0%, rgba(118, 75, 162, 0.05) 100%)' : 'var(--bg-white)',
                    transition: 'all 0.3s ease'
                  }}
                >
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: '16px' }}>
                    <div style={{ flex: 1 }}>
                      <div style={{
                        fontSize: '16px',
                        fontWeight: '600',
                        color: 'var(--text-primary)',
                        marginBottom: '8px'
                      }}>
                        재구성 작업
                      </div>
                      <div style={{
                        fontSize: '12px',
                        color: 'var(--text-secondary)',
                        marginBottom: '6px',
                        fontFamily: 'monospace'
                      }}>
                        ID: {recon.id.substring(0, 8)}...
                      </div>
                      <div style={{
                        fontSize: '14px',
                        color: 'var(--text-secondary)',
                        marginBottom: '8px'
                      }}>
                        생성 시간: {new Date(recon.created_at).toLocaleString('ko-KR')}
                      </div>
                      {recon.status === 'completed' && (
                        <div style={{
                          fontSize: '13px',
                          color: 'var(--primary-color)',
                          fontWeight: '500',
                          marginTop: '8px',
                          padding: '6px 12px',
                          background: 'rgba(102, 126, 234, 0.1)',
                          borderRadius: '6px',
                          display: 'inline-block'
                        }}>
                          ✓ 3D 메쉬 생성 완료
                        </div>
                      )}
                      {recon.dicom_url && (
                        <div style={{
                          fontSize: '12px',
                          color: 'var(--text-secondary)',
                          marginTop: '6px',
                          opacity: 0.7
                        }}>
                          파일: {recon.dicom_url.split(',').length}개
                        </div>
                      )}
                    </div>
                    <span style={{
                      padding: '8px 18px',
                      borderRadius: '20px',
                      background: getStatusColor(recon.status),
                      color: 'white',
                      fontSize: '13px',
                      fontWeight: '600',
                      textTransform: 'uppercase',
                      letterSpacing: '0.5px',
                      boxShadow: '0 2px 8px rgba(0,0,0,0.15)',
                      whiteSpace: 'nowrap'
                    }}>
                      {recon.status === 'pending' ? '대기중' :
                       recon.status === 'processing' ? '처리중' :
                       recon.status === 'completed' ? '완료' : '실패'}
                    </span>
                  </div>
                  
                  <div style={{ display: 'flex', gap: '12px', marginTop: '16px', flexWrap: 'wrap' }}>
                    {recon.status === 'completed' && (
                      <>
                        <button
                          onClick={() => navigate(`/viewer/${recon.id}`)}
                          style={{
                            padding: '10px 20px',
                            background: 'var(--primary-color)',
                            color: 'white',
                            border: 'none',
                            borderRadius: '8px',
                            cursor: 'pointer',
                            fontSize: '14px',
                            fontWeight: '500',
                            flex: 1,
                            minWidth: '120px',
                            transition: 'all 0.3s ease'
                          }}
                        >
                          DICOM 뷰어
                        </button>
                        <button
                          onClick={() => navigate(`/mesh/${recon.id}`)}
                          style={{
                            padding: '10px 20px',
                            background: 'linear-gradient(135deg, var(--primary-color) 0%, var(--primary-dark) 100%)',
                            color: 'white',
                            border: 'none',
                            borderRadius: '8px',
                            cursor: 'pointer',
                            fontSize: '14px',
                            fontWeight: '500',
                            flex: 1,
                            minWidth: '120px',
                            transition: 'all 0.3s ease'
                          }}
                        >
                          3D 메쉬 뷰어
                        </button>
                        <button
                          onClick={async () => {
                            try {
                              const response = await api.get(`/reconstruct/${recon.id}/download?format=stl`)
                              window.open(response.data.download_url, '_blank')
                            } catch (error) {
                              alert('다운로드 실패')
                            }
                          }}
                          style={{
                            padding: '10px 20px',
                            background: 'var(--bg-light)',
                            color: 'var(--text-primary)',
                            border: '1px solid var(--border-color)',
                            borderRadius: '8px',
                            cursor: 'pointer',
                            fontSize: '14px',
                            fontWeight: '500',
                            minWidth: '100px',
                            transition: 'all 0.3s ease'
                          }}
                        >
                          STL 다운로드
                        </button>
                        <button
                          onClick={async () => {
                            try {
                              const response = await api.get(`/reconstruct/${recon.id}/download?format=gltf`)
                              window.open(response.data.download_url, '_blank')
                            } catch (error) {
                              alert('다운로드 실패')
                            }
                          }}
                          style={{
                            padding: '10px 20px',
                            background: 'var(--bg-light)',
                            color: 'var(--text-primary)',
                            border: '1px solid var(--border-color)',
                            borderRadius: '8px',
                            cursor: 'pointer',
                            fontSize: '14px',
                            fontWeight: '500',
                            minWidth: '100px',
                            transition: 'all 0.3s ease'
                          }}
                        >
                          GLTF 다운로드
                        </button>
                      </>
                    )}
                    {recon.status === 'completed' && (
                      <button
                        onClick={async () => {
                          const label = prompt('세그멘테이션 레이블을 입력하세요 (예: brain, skull):')
                          if (!label) return
                          try {
                            await api.post(`/segmentation/${recon.id}?label=${encodeURIComponent(label)}`)
                            alert('세그멘테이션 작업이 시작되었습니다')
                            fetchReconstructions()
                          } catch (error) {
                            alert('세그멘테이션 시작 실패: ' + (error.response?.data?.detail || error.message))
                          }
                        }}
                        style={{
                          padding: '10px 20px',
                          background: 'linear-gradient(135deg, var(--secondary-color) 0%, var(--primary-color) 100%)',
                          color: 'white',
                          border: 'none',
                          borderRadius: '8px',
                          cursor: 'pointer',
                          fontSize: '14px',
                          fontWeight: '500',
                          minWidth: '140px',
                          transition: 'all 0.3s ease'
                        }}
                      >
                        AI 세그멘테이션
                      </button>
                    )}
                  </div>
                  
                  {recon.error_message && (
                    <div style={{
                      marginTop: '12px',
                      padding: '12px',
                      background: '#fff3cd',
                      borderRadius: '8px',
                      fontSize: '13px',
                      color: '#856404'
                    }}>
                      오류: {recon.error_message}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      </main>
    </div>
  )
}

export default Dashboard

