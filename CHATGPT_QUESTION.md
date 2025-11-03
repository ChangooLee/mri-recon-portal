# ChatGPT 질문용: Three.js 메쉬 뷰어 로딩바 및 원점 문제

## 문제 설명

1. **로딩바가 표시되지 않음**: GLB 파일(약 240MB) 다운로드 중 진행률이 화면에 표시되지 않음
2. **메쉬가 원점에 정확히 위치하지 않음**: 메쉬가 엉뚱한 곳에 있고 너무 크게 표시됨

## 기술 스택
- React 18.3.1
- Three.js (react-three/fiber, react-three/drei)
- GLTFLoader with DRACOLoader
- 파일 다운로드: fetch API with ReadableStream

## 핵심 코드

### 1. Model 컴포넌트 (로딩 및 메쉬 처리)

```jsx
import React, { useEffect, useState } from 'react'
import { Html } from '@react-three/drei'
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'
import { DRACOLoader } from 'three/examples/jsm/loaders/DRACOLoader.js'
import * as THREE from 'three'

const dracoLoader = new DRACOLoader()
dracoLoader.setDecoderPath('https://www.gstatic.com/draco/versioned/decoders/1.5.7/')

function Model({ url, onProgress, onLoaded }) {
  const [scene, setScene] = useState(null)
  const [loadingProgress, setLoadingProgress] = useState(0)
  
  useEffect(() => {
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
          const firstChunk = await reader.read()
          if (!firstChunk.done && firstChunk.value) {
            chunks.push(firstChunk.value)
            loaded += firstChunk.value.length
            estimatedTotal = firstChunk.value.length * 100
          }
        }
        
        while (true) {
          const { done, value } = await reader.read()
          if (done) break
          
          chunks.push(value)
          loaded += value.length
          
          // 진행률 업데이트
          let percent = 0
          if (total > 0) {
            percent = Math.min((loaded / total) * 100, 99)
          } else if (estimatedTotal > 0) {
            percent = Math.min((loaded / estimatedTotal) * 100, 95)
          } else {
            percent = Math.min(50, (loaded / 1000000) * 50)
          }
          
          setLoadingProgress(percent)
          if (onProgress) {
            onProgress(percent)
          }
        }
        
        const blob = new Blob(chunks)
        const blobUrl = URL.createObjectURL(blob)
        
        setLoadingProgress(95)
        if (onProgress) {
          onProgress(95)
        }
        
        loadGLTF(blobUrl, onProgress, onLoaded)
      } catch (error) {
        if (error.name !== 'AbortError') {
          console.error('Fetch error:', error)
        }
      }
    }
    
    const loadGLTF = (blobUrl, onProgress, onLoaded) => {
      const loader = new GLTFLoader()
      loader.setDRACOLoader(dracoLoader)
      
      loader.load(
        blobUrl,
        (gltf) => {
          const loadedScene = gltf.scene
          
          // 바운딩 박스 계산
          const box = new THREE.Box3().setFromObject(loadedScene)
          
          if (!box.isEmpty()) {
            const center = box.getCenter(new THREE.Vector3())
            const size = box.getSize(new THREE.Vector3())
            const maxDim = Math.max(size.x, size.y, size.z)
            
            // 메쉬를 원점(0,0,0)으로 이동
            loadedScene.position.set(-center.x, -center.y, -center.z)
            
            // 재질 설정
            loadedScene.traverse((child) => {
              if (child.isMesh) {
                child.castShadow = true
                child.receiveShadow = true
                child.frustumCulled = false
                child.visible = true
                
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
          // GLTFLoader progress
          if (progress.total > 0 && onProgress) {
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
  
  // 로딩바 표시
  if (!scene) {
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
              background: 'linear-gradient(90deg, #667eea, #764ba2)',
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
```

### 2. Canvas 및 OrbitControls 설정

```jsx
import { Canvas } from '@react-three/fiber'
import { OrbitControls, PerspectiveCamera } from '@react-three/drei'

function MeshViewer() {
  const [cameraDistance, setCameraDistance] = useState({ min: 0.1, max: 50 })
  const [loadProgress, setLoadProgress] = useState(0)
  const [meshLoaded, setMeshLoaded] = useState(false)
  
  const handleProgress = (percent) => {
    setLoadProgress(percent)
  }
  
  const handleMeshLoaded = (info) => {
    setMeshLoaded(true)
    if (info && info.maxDim) {
      const maxDim = info.maxDim
      const minDist = Math.max(0.1, maxDim * 0.5)
      const maxDist = Math.max(50, maxDim * 5)
      setCameraDistance({ min: minDist, max: maxDist })
    }
  }
  
  return (
    <Canvas 
      shadows
      gl={{ 
        preserveDrawingBuffer: true,
        powerPreference: "high-performance",
        antialias: true,
        alpha: false
      }}
    >
      <PerspectiveCamera makeDefault position={[3, 3, 3]} fov={50} />
      <ambientLight intensity={0.8} />
      <directionalLight position={[10, 10, 5]} intensity={1.5} castShadow />
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
    </Canvas>
  )
}
```

## 문제 증상

1. **로딩바 문제**:
   - `loadingProgress` state가 업데이트되지만 화면에 표시되지 않음
   - `Html` 컴포넌트가 Canvas 내부에 있어서 렌더링이 안 되는 것 같음
   - 브라우저 콘솔에 `loadingProgress` 값은 정상적으로 업데이트됨을 확인

2. **원점 문제**:
   - `loadedScene.position.set(-center.x, -center.y, -center.z)` 로 설정했지만
   - 메쉬가 여전히 원점 근처에 있지 않고 엉뚱한 곳에 표시됨
   - 그리드와 축 헬퍼는 원점에 정확히 있음
   - 메쉬가 너무 커서 카메라 범위 밖에 있을 수도 있음

## 질문

1. **로딩바가 화면에 표시되지 않는 이유는 무엇인가요?** `Html` 컴포넌트를 Canvas 내부에서 사용하는 것이 문제인가요?

2. **메쉬를 정확히 원점(0,0,0)에 배치하는 올바른 방법은 무엇인가요?** 바운딩 박스의 center를 계산해서 position을 설정했는데도 정확히 원점에 오지 않습니다.

3. **큰 메쉬(수백만 개의 면)를 로드할 때 진행률을 정확히 표시하는 방법은 무엇인가요?**

