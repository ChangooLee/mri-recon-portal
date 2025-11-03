import React, { useEffect, useRef, useState, Suspense } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useAuth } from '../services/auth'
import api from '../services/api'
import { Canvas, useThree } from '@react-three/fiber'
import { OrbitControls, PerspectiveCamera } from '@react-three/drei'
import * as THREE from 'three'
import { DRACOLoader } from 'three/examples/jsm/loaders/DRACOLoader.js'
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'
// @ts-ignore - meshopt_decoder 모듈
import { MeshoptDecoder } from 'three/examples/jsm/libs/meshopt_decoder.module.js'

// 로더/디코더 준비 (Draco+Meshopt, 워커 사전 로드)
const gltfLoader = new GLTFLoader()
const draco = new DRACOLoader()
draco.setDecoderPath('https://www.gstatic.com/draco/versioned/decoders/1.5.7/')
draco.setDecoderConfig({ type: 'wasm' })
draco.setWorkerLimit(2)
draco.preload() // 워커/WASM 사전 로드
gltfLoader.setDRACOLoader(draco)
gltfLoader.setMeshoptDecoder(MeshoptDecoder)
gltfLoader.setCrossOrigin('anonymous')

// fitToView 함수: 원점·스케일 정확히 맞추기
function fitToView(root, camera, controls, opts = { targetSize: 5 }) {
  root.updateMatrixWorld(true)

  // pivot 그룹에 모델을 넣고 pivot을 이동/스케일
  const pivot = new THREE.Group()
  pivot.add(root)

  // 초기 박스/단위 추정
  const box = new THREE.Box3().setFromObject(pivot)
  const size = box.getSize(new THREE.Vector3())
  const center = box.getCenter(new THREE.Vector3())
  const maxDim = Math.max(size.x, size.y, size.z)

  // 메쉬는 이미 m 단위로 변환됨 (백엔드에서 mm→m 변환 완료)
  // 따라서 추가 단위 변환 불필요, 바로 스케일만 조정
  const scale = (opts.targetSize ?? 5) / (maxDim || 1)
  pivot.scale.setScalar(scale)

  // 스케일 후 다시 중심 맞추기
  pivot.updateMatrixWorld(true)
  const box2 = new THREE.Box3().setFromObject(pivot)
  const center2 = box2.getCenter(new THREE.Vector3())
  pivot.position.sub(center2) // (0,0,0)로 이동

  // 카메라 맞추기
  const size2 = box2.getSize(new THREE.Vector3())
  const maxDim2 = Math.max(size2.x, size2.y, size2.z)
  const fov = THREE.MathUtils.degToRad(camera.fov)
  const dist = maxDim2 / (2 * Math.tan(fov / 2))
  const offset = 1.3

  const viewDir = new THREE.Vector3(1, 1, 1).normalize()
  camera.position.copy(center2).addScaledVector(viewDir, dist * offset)
  camera.near = dist / 100
  camera.far = dist * 100
  camera.updateProjectionMatrix()

  controls.target.copy(new THREE.Vector3(0, 0, 0))
  controls.minDistance = dist / 10
  controls.maxDistance = dist * 10
  controls.update()

  return { pivot, box: box2, maxDim: maxDim2 }
}

function Model({ url, onProgress, onLoaded, controlsRef, cameraRef }) {
  const [scene, setScene] = useState(null)
  
  useEffect(() => {
    let isAborted = false
    
    // GLTFLoader의 onProgress로 다운로드 진행률 받기
    gltfLoader.load(
      url,
      (gltf) => {
        if (isAborted) return
        
        const root = gltf.scene
        
        // 재질 및 그림자 설정
        root.traverse((child) => {
          if (child.isMesh) {
            child.castShadow = true
            child.receiveShadow = true
            child.frustumCulled = false
            
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
        
        root.visible = true
        
        // fitToView로 원점·스케일 정확히 맞추기
        if (controlsRef?.current && cameraRef?.current) {
          const { pivot, maxDim } = fitToView(root, cameraRef.current, controlsRef.current, { targetSize: 5 })
          
          console.log('Scene fitted:', {
            maxDim: maxDim,
            position: pivot.position,
            scale: pivot.scale
          })
          
          setScene(pivot)
          
          if (onLoaded) {
            onLoaded({ maxDim })
          }
        } else {
          // controls/camera가 아직 없으면 기본 처리
          setScene(root)
          if (onLoaded) {
            onLoaded({ root })
          }
        }
      },
      (progress) => {
        // GLTFLoader의 onProgress: 다운로드 진행률 (0-90%)
        if (progress.total > 0 && onProgress) {
          const percent = (progress.loaded / progress.total) * 90 // 다운로드 0-90%
          onProgress(percent)
        }
      },
      (error) => {
        console.error('GLTF loading error:', error)
      }
    )
    
    return () => {
      isAborted = true
    }
  }, [url, onProgress, onLoaded, controlsRef, cameraRef])
  
  // Canvas 외부에서 로딩 표시하지 않음 (DOM 오버레이 사용)
  if (!scene) return null
  
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
  const [loadStage, setLoadStage] = useState('download') // 'download' | 'parse'
  const containerRef = useRef(null)
  const cameraRef = useRef(null)
  const controlsRef = useRef(null)

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
    // 90% 이상이면 파싱 단계
    if (percent >= 90) {
      setLoadStage('parse')
    }
  }
  
  const handleMeshLoaded = (info) => {
    setMeshLoaded(true)
    setLoadProgress(100)
    console.log('Mesh loaded:', info)
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
        {/* Canvas 외부 DOM 오버레이 로딩바 */}
        {!meshLoaded && loadProgress > 0 && (
          <div style={{
            position: 'absolute',
            top: '50%',
            left: '50%',
            transform: 'translate(-50%, -50%)',
            zIndex: 1000,
            background: 'rgba(0, 0, 0, 0.9)',
            color: 'white',
            padding: '30px 40px',
            borderRadius: '12px',
            textAlign: 'center',
            minWidth: '300px',
            boxShadow: '0 4px 20px rgba(0,0,0,0.5)'
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
                width: `${loadProgress}%`,
                height: '100%',
                background: 'linear-gradient(90deg, var(--primary-color), var(--primary-dark))',
                transition: 'width 0.3s ease',
                borderRadius: '6px'
              }} />
            </div>
            <div style={{ fontSize: '16px', fontWeight: '500', marginBottom: '8px' }}>
              {loadProgress.toFixed(1)}%
            </div>
            <div style={{ fontSize: '12px', opacity: 0.7 }}>
              {loadStage === 'download' ? '파일 다운로드 중...' : '메쉬 파싱/디코딩 중...'}
            </div>
            {fileSize && (
              <div style={{ fontSize: '11px', opacity: 0.6, marginTop: '8px' }}>
                파일 크기: {fileSize} MB
              </div>
            )}
          </div>
        )}
        
        {meshUrl && (
          <Canvas 
            shadows
            frameloop="always"
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
            <PerspectiveCamera ref={cameraRef} makeDefault position={[3, 3, 3]} fov={50} />
            <ambientLight intensity={0.8} />
            <directionalLight position={[10, 10, 5]} intensity={1.5} castShadow />
            <pointLight position={[-10, -10, -5]} intensity={0.5} />
            <OrbitControls
              ref={controlsRef}
              enablePan={true}
              enableZoom={true}
              enableRotate={true}
              target={[0, 0, 0]}
            />
            <gridHelper args={[10, 10]} />
            <axesHelper args={[5]} />
            <Suspense fallback={null}>
              {meshUrl && (
                <Model 
                  url={meshUrl} 
                  onProgress={handleProgress} 
                  onLoaded={handleMeshLoaded}
                  controlsRef={controlsRef}
                  cameraRef={cameraRef}
                />
              )}
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
