import React, { useEffect, useRef, useState, Suspense, useMemo } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useAuth } from '../services/auth'
import api from '../services/api'
import { Canvas } from '@react-three/fiber'
import { OrbitControls, PerspectiveCamera, useGLTF, Html } from '@react-three/drei'
import * as THREE from 'three'
import { DRACOLoader } from 'three/examples/jsm/loaders/DRACOLoader.js'
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'

// Draco 로더 설정 (압축된 GLB 디코딩용)
const dracoLoader = new DRACOLoader()
dracoLoader.setDecoderPath('https://www.gstatic.com/draco/versioned/decoders/1.5.7/')
dracoLoader.setDecoderConfig({ type: 'js' })

// GLTFLoader에 Draco 로더 설정
const gltfLoader = new GLTFLoader()
gltfLoader.setDRACOLoader(dracoLoader)

function Model({ url, onProgress, onLoaded }) {
  const [scene, setScene] = useState(null)
  const [loadingProgress, setLoadingProgress] = useState(0)
  
  useEffect(() => {
    // 직접 fetch를 사용하여 progress 추적
    let abortController = new AbortController()
    
    const loadModel = async () => {
      try {
        const response = await fetch(url, { signal: abortController.signal })
        const total = parseInt(response.headers.get('Content-Length') || '0')
        const reader = response.body.getReader()
        const chunks = []
        let loaded = 0
        let estimatedTotal = total
        
        // Content-Length가 없으면 초기 청크로 추정
        if (total === 0) {
          // 첫 번째 청크를 읽어서 크기 추정 시도
          const firstChunk = await reader.read()
          if (!firstChunk.done && firstChunk.value) {
            chunks.push(firstChunk.value)
            loaded += firstChunk.value.length
            // 첫 청크 크기의 100배로 추정 (나중에 업데이트됨)
            estimatedTotal = firstChunk.value.length * 100
          }
        }
        
        while (true) {
          const { done, value } = await reader.read()
          if (done) break
          
          chunks.push(value)
          loaded += value.length
          
          // 진행률 업데이트 (Content-Length가 없어도 추정값 사용)
          let percent = 0
          if (total > 0) {
            percent = Math.min((loaded / total) * 100, 99) // 99%까지 (GLTF 파싱 1% 남김)
          } else if (estimatedTotal > 0) {
            // 추정값 사용하되 95%까지만 (나머지는 GLTF 파싱)
            percent = Math.min((loaded / estimatedTotal) * 100, 95)
          } else {
            // 추정값도 없으면 로딩 중으로만 표시
            percent = Math.min(50, (loaded / 1000000) * 50) // 1MB당 50%로 가정
          }
          
          setLoadingProgress(percent)
          if (onProgress) {
            onProgress(percent)
          }
        }
        
        // 모든 청크를 하나로 합치기
        const blob = new Blob(chunks)
        const blobUrl = URL.createObjectURL(blob)
        
        // 다운로드 완료 표시
        setLoadingProgress(95)
        if (onProgress) {
          onProgress(95)
        }
        
        loadGLTF(blobUrl, onProgress, onLoaded)
        
        return () => {
          if (blobUrl) URL.revokeObjectURL(blobUrl)
        }
      } catch (error) {
        if (error.name !== 'AbortError') {
          console.error('Fetch error:', error)
        }
      }
    }
    
    const loadGLTF = (blobUrl, onProgress, onLoaded) => {
      // GLTFLoader로 로드
      const loader = new GLTFLoader()
      loader.setDRACOLoader(dracoLoader)
      
      loader.load(
        blobUrl,
        (gltf) => {
            const loadedScene = gltf.scene
            
            // 바운딩 박스 계산하여 자동 스케일 및 카메라 조정
            const box = new THREE.Box3().setFromObject(loadedScene)
            
            if (!box.isEmpty()) {
              const center = box.getCenter(new THREE.Vector3())
              const size = box.getSize(new THREE.Vector3())
              const maxDim = Math.max(size.x, size.y, size.z)
              
              // 메쉬를 정확히 원점(0,0,0)으로 이동
              loadedScene.position.set(-center.x, -center.y, -center.z)
              
              // 재질 및 그림자 설정
              loadedScene.traverse((child) => {
                if (child.isMesh) {
                  child.castShadow = true
                  child.receiveShadow = true
                  child.frustumCulled = false
                  
                  // 재질 설정
                  if (!child.material) {
                    child.material = new THREE.MeshStandardMaterial({
                      color: 0x667eea,
                      metalness: 0.1,
                      roughness: 0.5
                    })
                  } else {
                    if (Array.isArray(child.material)) {
                      child.material.forEach(mat => {
                        if (mat) {
                          mat.needsUpdate = true
                          mat.visible = true
                        }
                      })
                    } else {
                      child.material.needsUpdate = true
                      child.material.visible = true
                    }
                  }
                  
                  child.visible = true
                }
              })
              
              loadedScene.visible = true
              
              console.log('Scene loaded:', {
                center: { x: center.x, y: center.y, z: center.z },
                size: { x: size.x, y: size.y, z: size.z },
                maxDim: maxDim,
                position: loadedScene.position
              })
              
              setScene(loadedScene)
              
              if (onLoaded) {
                onLoaded({ center, size, maxDim })
              }
            } else {
              console.error('Empty bounding box')
              setScene(loadedScene)
            }
            
          URL.revokeObjectURL(blobUrl)
        },
        (progress) => {
          // GLTFLoader의 progress는 모델 파싱 진행률 (파일 다운로드와는 별개)
          if (progress.total > 0 && onProgress) {
            // 다운로드는 이미 완료되었으므로 90-100% 범위로 표시
            const percent = 90 + (progress.loaded / progress.total) * 10
            setLoadingProgress(percent)
            onProgress(percent)
          }
        },
        (error) => {
          console.error('GLTF loading error:', error)
          URL.revokeObjectURL(blobUrl)
        }
      )
    }
    
    loadModel()
    
    return () => {
      abortController.abort()
    }
  }, [url, onProgress, onLoaded])
  
  if (!scene) {
    // 로딩 중 progress 표시
    return (
      <Html center>
        <div style={{
          background: 'rgba(0, 0, 0, 0.9)',
          color: 'white',
          padding: '30px 40px',
          borderRadius: '12px',
          textAlign: 'center',
          minWidth: '300px'
        }}>
          <div style={{ marginBottom: '20px', fontSize: '18px', fontWeight: '600' }}>
            3D 메쉬 로딩 중...
          </div>
          <div style={{
            width: '280px',
            height: '12px',
            background: 'rgba(255, 255, 255, 0.2)',
            borderRadius: '6px',
            overflow: 'hidden',
            margin: '0 auto 15px'
          }}>
            <div style={{
              width: `${loadingProgress}%`,
              height: '100%',
              background: 'linear-gradient(90deg, var(--primary-color), var(--primary-dark))',
              transition: 'width 0.3s ease',
              borderRadius: '6px'
            }} />
          </div>
          <div style={{ fontSize: '16px', fontWeight: '500', marginBottom: '8px' }}>
            {loadingProgress.toFixed(1)}%
          </div>
          <div style={{ fontSize: '12px', opacity: 0.7 }}>
            {loadingProgress < 95 ? '파일 다운로드 중...' : '메쉬 파싱 중...'}
          </div>
        </div>
      </Html>
    )
  }
  
  return <primitive object={scene} />
}

function MeshViewer() {
  const { reconstructionId } = useParams()
  const navigate = useNavigate()
  const { user } = useAuth()
  const [loading, setLoading] = useState(true)
  const [meshUrl, setMeshUrl] = useState(null)
  const [stlUrl, setStlUrl] = useState(null)
  const [loadProgress, setLoadProgress] = useState(0)
  const [fileSize, setFileSize] = useState(null)
  const [meshLoaded, setMeshLoaded] = useState(false)
  const [error, setError] = useState(null)
  const containerRef = useRef(null)
  const cameraRef = useRef(null)

  useEffect(() => {
    if (!user) {
      navigate('/login')
      return
    }

    fetchReconstruction()
  }, [reconstructionId, user, navigate])

  const fetchReconstruction = async () => {
    try {
      const response = await api.get(`/reconstruct/${reconstructionId}`)
      setMeshUrl(response.data.gltf_url)
      setStlUrl(response.data.stl_url)
      
      // 파일 크기 확인 (정보 제공용)
      if (response.data.gltf_url) {
        try {
          const headResponse = await fetch(response.data.gltf_url, { method: 'HEAD' })
          const contentLength = headResponse.headers.get('Content-Length')
          if (contentLength) {
            const sizeMB = (parseInt(contentLength) / (1024 * 1024)).toFixed(1)
            setFileSize(sizeMB)
            // 파일 크기 경고 제거 - 사용자가 기다릴 수 있음
          }
        } catch (e) {
          console.warn('Failed to get file size:', e)
        }
      }
      
      setLoading(false)
    } catch (error) {
      console.error('Failed to fetch reconstruction:', error)
      setError('재구성 정보를 불러올 수 없습니다.')
      setLoading(false)
    }
  }
  
  const handleProgress = (percent) => {
    setLoadProgress(percent)
  }
  
  const handleMeshLoaded = (info) => {
    setMeshLoaded(true)
    console.log('Mesh loaded:', info)
    // 메쉬 크기에 따라 카메라 거리 조정
    if (info && info.maxDim) {
      const maxDim = info.maxDim
      // OrbitControls 거리 범위를 메쉬 크기의 0.5배부터 5배까지로 설정
      const minDist = Math.max(0.1, maxDim * 0.5)
      const maxDist = Math.max(50, maxDim * 5)
      setCameraDistance({ min: minDist, max: maxDist })
    }
  }
  
  const [cameraDistance, setCameraDistance] = useState({ min: 0.1, max: 50 })

  if (loading) {
    return (
      <div style={{ height: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center', background: 'var(--bg-light)' }}>
        <div style={{ textAlign: 'center' }}>
          <div style={{ fontSize: '18px', color: 'var(--text-primary)', marginBottom: '10px' }}>3D 메쉬 로딩 중...</div>
          <div style={{ fontSize: '14px', color: 'var(--text-secondary)' }}>Reconstruction ID: {reconstructionId?.substring(0, 8)}...</div>
        </div>
      </div>
    )
  }

  if (!meshUrl) {
    return (
      <div style={{ padding: '20px', height: '100vh', display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', background: 'var(--bg-light)' }}>
        <p style={{ marginBottom: '20px', color: 'var(--text-primary)' }}>3D 메쉬를 불러올 수 없습니다.</p>
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
            3D 메쉬 뷰어
          </h2>
          <p style={{ margin: '4px 0 0 0', fontSize: '12px', color: 'var(--text-secondary)' }}>
            Reconstruction ID: {reconstructionId?.substring(0, 8)}...
          </p>
        </div>
        <div style={{ display: 'flex', gap: '10px' }}>
          {stlUrl && (
            <button
              onClick={async () => {
                try {
                  const response = await api.get(`/reconstruct/${reconstructionId}/download?format=stl`)
                  window.open(response.data.download_url, '_blank')
                } catch (error) {
                  alert('다운로드 실패')
                }
              }}
              style={{
                padding: '8px 16px',
                background: 'var(--bg-light)',
                color: 'var(--text-primary)',
                border: '1px solid var(--border-color)',
                borderRadius: '8px',
                cursor: 'pointer',
                fontWeight: '500',
                fontSize: '13px'
              }}
            >
              STL 다운로드
            </button>
          )}
          <button
            onClick={async () => {
              try {
                const response = await api.get(`/reconstruct/${reconstructionId}/download?format=gltf`)
                window.open(response.data.download_url, '_blank')
              } catch (error) {
                alert('다운로드 실패')
              }
            }}
            style={{
              padding: '8px 16px',
              background: 'var(--bg-light)',
              color: 'var(--text-primary)',
              border: '1px solid var(--border-color)',
              borderRadius: '8px',
              cursor: 'pointer',
              fontWeight: '500',
              fontSize: '13px'
            }}
          >
            GLTF 다운로드
          </button>
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
        </div>
      </header>
      <div
        ref={containerRef}
        style={{
          flex: 1,
          background: '#1a1a1a',
          width: '100%',
          height: '100%',
          position: 'relative',
          overflow: 'hidden'
        }}
      >
        {meshUrl && (
          <Canvas 
            shadows
            gl={{ 
              preserveDrawingBuffer: true,
              powerPreference: "high-performance",
              antialias: true,
              alpha: false
            }}
            onCreated={({ gl }) => {
              // WebGL Context Lost 에러 방지
              gl.domElement.addEventListener('webglcontextlost', (event) => {
                event.preventDefault()
                console.warn('WebGL context lost, will restore')
              })
              gl.domElement.addEventListener('webglcontextrestored', () => {
                console.log('WebGL context restored')
                gl.setRenderTarget(null)
              })
            }}
          >
            <Suspense fallback={null}>
              <PerspectiveCamera ref={cameraRef} makeDefault position={[3, 3, 3]} fov={50} />
              <ambientLight intensity={0.8} />
              <directionalLight position={[10, 10, 5]} intensity={1.5} castShadow />
              <pointLight position={[-10, -10, -5]} intensity={0.5} />
              <Model url={meshUrl} onProgress={handleProgress} onLoaded={handleMeshLoaded} />
              <OrbitControls
                enablePan={true}
                enableZoom={true}
                enableRotate={true}
                minDistance={cameraDistance.min}
                maxDistance={cameraDistance.max}
                target={[0, 0, 0]}
              />
              <gridHelper args={[10, 10]} />
              <axesHelper args={[5]} />
            </Suspense>
          </Canvas>
        )}
        {error && (
          <div style={{
            position: 'absolute',
            top: '20px',
            left: '50%',
            transform: 'translateX(-50%)',
            background: 'rgba(220, 53, 69, 0.9)',
            color: 'white',
            padding: '12px 20px',
            borderRadius: '8px',
            fontSize: '14px',
            fontWeight: '500',
            zIndex: 1000,
            maxWidth: '80%',
            textAlign: 'center'
          }}>
            {error}
          </div>
        )}
        <div style={{
          position: 'absolute',
          bottom: '20px',
          left: '20px',
          background: 'rgba(0, 0, 0, 0.7)',
          color: 'white',
          padding: '10px 15px',
          borderRadius: '8px',
          fontSize: '12px',
          fontFamily: 'monospace'
        }}>
          <div>마우스 드래그: 회전</div>
          <div>마우스 휠: 줌</div>
          <div>마우스 우클릭 + 드래그: 이동</div>
          {!meshLoaded && loadProgress > 0 && loadProgress < 100 && (
            <div style={{ marginTop: '10px', paddingTop: '10px', borderTop: '1px solid rgba(255,255,255,0.3)' }}>
              <div style={{ marginBottom: '5px' }}>로딩 중: {loadProgress.toFixed(1)}%</div>
              <div style={{
                width: '100%',
                height: '4px',
                background: 'rgba(255,255,255,0.2)',
                borderRadius: '2px',
                overflow: 'hidden'
              }}>
                <div style={{
                  width: `${loadProgress}%`,
                  height: '100%',
                  background: 'rgba(102, 126, 234, 0.8)',
                  transition: 'width 0.3s ease'
                }} />
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

export default MeshViewer
