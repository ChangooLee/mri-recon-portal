import React, { useEffect, useRef, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useAuth } from '../services/auth'
import api from '../services/api'

function Viewer() {
  const { reconstructionId } = useParams()
  const navigate = useNavigate()
  const { user } = useAuth()
  const [loading, setLoading] = useState(true)
  const [dicomInfo, setDicomInfo] = useState(null)
  const [currentSlice, setCurrentSlice] = useState(0)
  const [sliceImage, setSliceImage] = useState(null)
  const [windowCenter, setWindowCenter] = useState(null)
  const [windowWidth, setWindowWidth] = useState(null)
  const viewerRef = useRef(null)

  useEffect(() => {
    if (!user) {
      navigate('/login')
      return
    }

    fetchDicomInfo()
  }, [reconstructionId, user, navigate])

  useEffect(() => {
    if (dicomInfo && currentSlice >= 0) {
      loadSlice(currentSlice)
    }
    // cleanup image URL
    return () => {
      if (sliceImage) {
        URL.revokeObjectURL(sliceImage)
      }
    }
  }, [dicomInfo, currentSlice, windowCenter, windowWidth])

  const fetchDicomInfo = async () => {
    try {
      const response = await api.get(`/viewer/${reconstructionId}/info`)
      setDicomInfo(response.data)
      setCurrentSlice(0)
      setLoading(false)
    } catch (error) {
      console.error('Failed to fetch DICOM info:', error)
      setLoading(false)
    }
  }

  const loadSlice = async (sliceIndex) => {
    try {
      const params = {}
      if (windowCenter !== null) params.window_center = windowCenter
      if (windowWidth !== null) params.window_width = windowWidth
      
      const response = await api.get(`/viewer/${reconstructionId}/slice/${sliceIndex}`, {
        params,
        responseType: 'blob'
      })
      
      const imageUrl = URL.createObjectURL(response.data)
      if (sliceImage) {
        URL.revokeObjectURL(sliceImage)
      }
      setSliceImage(imageUrl)
    } catch (error) {
      console.error('Failed to load slice:', error)
      setSliceImage(null)
    }
  }

  const handlePreviousSlice = () => {
    if (currentSlice > 0) {
      setCurrentSlice(currentSlice - 1)
    }
  }

  const handleNextSlice = () => {
    if (dicomInfo && currentSlice < dicomInfo.total_slices - 1) {
      setCurrentSlice(currentSlice + 1)
    }
  }

  const handleSliceChange = (e) => {
    const newSlice = parseInt(e.target.value)
    if (newSlice >= 0 && dicomInfo && newSlice < dicomInfo.total_slices) {
      setCurrentSlice(newSlice)
    }
  }

  if (loading) {
    return (
      <div style={{ height: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center', background: 'var(--bg-light)' }}>
        <div style={{ textAlign: 'center' }}>
          <div style={{ fontSize: '18px', color: 'var(--text-primary)', marginBottom: '10px' }}>DICOM 정보 로딩 중...</div>
          <div style={{ fontSize: '14px', color: 'var(--text-secondary)' }}>Reconstruction ID: {reconstructionId?.substring(0, 8)}...</div>
        </div>
      </div>
    )
  }

  if (!dicomInfo) {
    return (
      <div style={{ padding: '20px', height: '100vh', display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', background: 'var(--bg-light)' }}>
        <p style={{ marginBottom: '20px', color: 'var(--text-primary)' }}>DICOM 파일을 불러올 수 없습니다.</p>
        <button
          onClick={() => navigate('/dashboard')}
          style={{
            padding: '10px 20px',
            background: 'var(--primary-color)',
            color: 'white',
            border: 'none',
            borderRadius: '8px',
            cursor: 'pointer',
            fontWeight: '500',
            fontSize: '14px'
          }}
        >
          대시보드로 돌아가기
        </button>
      </div>
    )
  }

  return (
    <div style={{ height: '100vh', display: 'flex', flexDirection: 'column', background: 'var(--bg-light)' }}>
      <header style={{
        background: 'var(--bg-white)',
        padding: '15px 30px',
        boxShadow: 'var(--shadow)',
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        borderBottom: '1px solid var(--border-color)'
      }}>
        <div>
          <h2 style={{
            margin: 0,
            fontSize: '20px',
            fontWeight: '700',
            background: 'linear-gradient(135deg, var(--primary-color) 0%, var(--primary-dark) 100%)',
            WebkitBackgroundClip: 'text',
            WebkitTextFillColor: 'transparent'
          }}>
            DICOM 뷰어
          </h2>
          <div style={{ margin: '4px 0 0 0', fontSize: '12px', color: 'var(--text-secondary)' }}>
            {dicomInfo.patient_name && <span>환자: {dicomInfo.patient_name} | </span>}
            슬라이스: {currentSlice + 1} / {dicomInfo.total_slices}
            {dicomInfo.study_date && <span> | 스캔 날짜: {dicomInfo.study_date}</span>}
          </div>
        </div>
        <button
          onClick={() => navigate('/dashboard')}
          style={{
            padding: '10px 20px',
            background: 'var(--primary-color)',
            color: 'white',
            border: 'none',
            borderRadius: '8px',
            cursor: 'pointer',
            fontWeight: '500',
            fontSize: '14px',
            transition: 'all 0.3s ease'
          }}
          onMouseEnter={(e) => {
            e.currentTarget.style.background = 'var(--primary-dark)'
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.background = 'var(--primary-color)'
          }}
        >
          대시보드로 돌아가기
        </button>
      </header>

      <div style={{ flex: 1, display: 'flex', background: '#000', position: 'relative', overflow: 'hidden' }}>
        {/* 사이드바 - 컨트롤 */}
        <div style={{
          width: '280px',
          background: 'var(--bg-white)',
          padding: '20px',
          borderRight: '1px solid var(--border-color)',
          display: 'flex',
          flexDirection: 'column',
          gap: '20px',
          overflowY: 'auto'
        }}>
          <div>
            <label style={{ display: 'block', marginBottom: '8px', fontSize: '14px', fontWeight: '600', color: 'var(--text-primary)' }}>
              슬라이스 탐색
            </label>
            <div style={{ display: 'flex', gap: '10px', alignItems: 'center', marginBottom: '10px' }}>
              <button
                onClick={handlePreviousSlice}
                disabled={currentSlice === 0}
                style={{
                  padding: '8px 16px',
                  background: currentSlice === 0 ? '#ccc' : 'var(--primary-color)',
                  color: 'white',
                  border: 'none',
                  borderRadius: '6px',
                  cursor: currentSlice === 0 ? 'not-allowed' : 'pointer',
                  fontSize: '14px',
                  fontWeight: '500'
                }}
              >
                ← 이전
              </button>
              <button
                onClick={handleNextSlice}
                disabled={currentSlice >= dicomInfo.total_slices - 1}
                style={{
                  padding: '8px 16px',
                  background: currentSlice >= dicomInfo.total_slices - 1 ? '#ccc' : 'var(--primary-color)',
                  color: 'white',
                  border: 'none',
                  borderRadius: '6px',
                  cursor: currentSlice >= dicomInfo.total_slices - 1 ? 'not-allowed' : 'pointer',
                  fontSize: '14px',
                  fontWeight: '500'
                }}
              >
                다음 →
              </button>
            </div>
            <input
              type="range"
              min="0"
              max={dicomInfo.total_slices - 1}
              value={currentSlice}
              onChange={handleSliceChange}
              style={{ width: '100%', cursor: 'pointer' }}
            />
            <div style={{ textAlign: 'center', marginTop: '8px', fontSize: '12px', color: 'var(--text-secondary)' }}>
              슬라이스 {currentSlice + 1} / {dicomInfo.total_slices}
            </div>
          </div>

          <div>
            <label style={{ display: 'block', marginBottom: '8px', fontSize: '14px', fontWeight: '600', color: 'var(--text-primary)' }}>
              윈도우 조정 (Windowing)
            </label>
            <div style={{ marginBottom: '10px' }}>
              <label style={{ fontSize: '12px', color: 'var(--text-secondary)', display: 'block', marginBottom: '4px' }}>
                Window Center
              </label>
              <input
                type="number"
                value={windowCenter || ''}
                onChange={(e) => setWindowCenter(e.target.value ? parseFloat(e.target.value) : null)}
                placeholder="자동"
                style={{
                  width: '100%',
                  padding: '6px 10px',
                  border: '1px solid var(--border-color)',
                  borderRadius: '6px',
                  fontSize: '14px'
                }}
              />
            </div>
            <div>
              <label style={{ fontSize: '12px', color: 'var(--text-secondary)', display: 'block', marginBottom: '4px' }}>
                Window Width
              </label>
              <input
                type="number"
                value={windowWidth || ''}
                onChange={(e) => setWindowWidth(e.target.value ? parseFloat(e.target.value) : null)}
                placeholder="자동"
                style={{
                  width: '100%',
                  padding: '6px 10px',
                  border: '1px solid var(--border-color)',
                  borderRadius: '6px',
                  fontSize: '14px'
                }}
              />
            </div>
            <button
              onClick={() => {
                setWindowCenter(null)
                setWindowWidth(null)
              }}
              style={{
                marginTop: '10px',
                padding: '6px 12px',
                background: 'var(--bg-light)',
                color: 'var(--text-primary)',
                border: '1px solid var(--border-color)',
                borderRadius: '6px',
                cursor: 'pointer',
                fontSize: '12px',
                width: '100%'
              }}
            >
              자동으로 리셋
            </button>
          </div>

          {dicomInfo.modality && (
            <div>
              <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginBottom: '4px' }}>모달리티</div>
              <div style={{ fontSize: '14px', fontWeight: '500', color: 'var(--text-primary)' }}>{dicomInfo.modality}</div>
            </div>
          )}
        </div>

        {/* 이미지 뷰어 */}
        <div ref={viewerRef} style={{ flex: 1, background: '#000', display: 'flex', alignItems: 'center', justifyContent: 'center', position: 'relative', overflow: 'auto' }}>
          {sliceImage ? (
            <img
              src={sliceImage}
              alt={`DICOM Slice ${currentSlice + 1}`}
              style={{
                maxWidth: '100%',
                maxHeight: '100%',
                objectFit: 'contain',
                imageRendering: 'pixelated'
              }}
            />
          ) : (
            <div style={{ color: 'white', textAlign: 'center' }}>
              <p>이미지 로딩 중...</p>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

export default Viewer
