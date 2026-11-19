<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import * as THREE from 'three'
import { OrbitControls } from 'three/addons/controls/OrbitControls.js'

// H2 建图实时显示：轮询后端转发的降采样点云、轨迹与 SLAM 位姿并渲染。
// 坐标约定：ROS 为 x 前、y 左、z 上；渲染映射为 three 的 (x, z, -y)。
// 点云/位姿/轨迹分别用不同周期轮询：点云大（1.5s）、位姿小（0.7s）、轨迹慢（6s）。

const host = ref<HTMLDivElement>()
const statusLine = ref('等待建图数据接入')

let disposed = false
let renderer: THREE.WebGLRenderer | undefined
let controls: OrbitControls | undefined
let scene: THREE.Scene | undefined
let camera: THREE.PerspectiveCamera | undefined
let pointsMesh: THREE.Points | undefined
let pathLine: THREE.Line | undefined
let robotMesh: THREE.Mesh | undefined
let mapTimer: ReturnType<typeof setInterval> | undefined
let poseTimer: ReturnType<typeof setInterval> | undefined
let pathTimer: ReturnType<typeof setInterval> | undefined
let resizeObserver: ResizeObserver | undefined
let frameHandle = 0
let cameraFitted = false

async function fetchBinary(path: string): Promise<{ buf: ArrayBuffer; headers: Headers } | null> {
  try {
    const response = await fetch(path, { cache: 'no-store' })
    if (!response.ok) return null
    return { buf: await response.arrayBuffer(), headers: response.headers }
  } catch { return null }
}

function decodeCloud(buf: ArrayBuffer): Float32Array {
  // int16 毫米 → float 米
  const src = new Int16Array(buf)
  const out = new Float32Array(src.length)
  for (let i = 0; i < src.length; i++) out[i] = src[i] / 1000
  return out
}

function rebuildPoints(f32: Float32Array) {
  if (!scene) return
  const count = f32.length / 3
  if (!count) return
  const positions = new Float32Array(count * 3)
  const colors = new Float32Array(count * 3)
  let zMin = Number.POSITIVE_INFINITY
  let zMax = Number.NEGATIVE_INFINITY
  for (let i = 2; i < f32.length; i += 3) {
    if (f32[i]! < zMin) zMin = f32[i]!
    if (f32[i]! > zMax) zMax = f32[i]!
  }
  const zSpan = Math.max(0.5, zMax - zMin)
  const tint = new THREE.Color()
  for (let i = 0; i < count; i++) {
    const x = f32[3 * i]!, y = f32[3 * i + 1]!, z = f32[3 * i + 2]!
    positions[3 * i] = x
    positions[3 * i + 1] = z
    positions[3 * i + 2] = -y
    // 低处蓝、高处暖黄，机房结构层次一眼可辨
    const grade = (z - zMin) / zSpan
    tint.setHSL(0.58 - 0.5 * grade, 0.8, 0.34 + 0.3 * grade)
    colors[3 * i] = tint.r
    colors[3 * i + 1] = tint.g
    colors[3 * i + 2] = tint.b
  }
  const geometry = new THREE.BufferGeometry()
  geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3))
  geometry.setAttribute('color', new THREE.BufferAttribute(colors, 3))
  geometry.computeBoundingSphere()
  if (pointsMesh) {
    pointsMesh.geometry.dispose()
    scene.remove(pointsMesh)
  }
  pointsMesh = new THREE.Points(geometry, new THREE.PointsMaterial({ size: 0.05, vertexColors: true }))
  scene.add(pointsMesh)
  if (!cameraFitted && geometry.boundingSphere) {
    fitCamera(geometry.boundingSphere.center, geometry.boundingSphere.radius)
    cameraFitted = true
  }
}

function fitCamera(center: THREE.Vector3, radius: number) {
  if (!camera || !controls) return
  controls.target.copy(center)
  const distance = Math.max(4, radius * 2.4)
  camera.position.set(center.x + distance * 0.65, center.y + distance * 0.85, center.z + distance * 0.85)
  camera.near = 0.1
  camera.far = distance * 30
  camera.updateProjectionMatrix()
  controls.update()
}

function rebuildPath(buf: ArrayBuffer) {
  if (!scene) return
  const f32 = decodeCloud(buf)
  const vertices: THREE.Vector3[] = []
  for (let i = 0; i + 2 < f32.length; i += 3) vertices.push(new THREE.Vector3(f32[i]!, f32[i + 2]!, -f32[i + 1]!))
  if (!vertices.length) return
  if (pathLine) {
    pathLine.geometry.dispose()
    scene.remove(pathLine)
  }
  pathLine = new THREE.Line(
    new THREE.BufferGeometry().setFromPoints(vertices),
    new THREE.LineBasicMaterial({ color: 0xe8896f }))
  scene.add(pathLine)
}

function placeRobot(pose: { x: number; y: number; z?: number; yaw: number }) {
  if (!scene || !robotMesh) return
  robotMesh.position.set(pose.x, pose.z ?? 0, -pose.y)
  robotMesh.rotation.y = pose.yaw
}

async function pollMap() {
  if (disposed) return
  const result = await fetchBinary('/api/field/map/map')
  if (!result) {
    statusLine.value = '等待建图启动（PC3 建图桥未就绪）'
    return
  }
  rebuildPoints(decodeCloud(result.buf))
  const points = Number(result.headers.get('X-Map-Points'))
  const voxel = result.headers.get('X-Map-Voxel')
  const ageMs = Number(result.headers.get('X-Map-Age-Ms'))
  const pointsText = Number.isFinite(points) ? `${(points / 10000).toFixed(1)} 万点` : ''
  const voxelText = voxel ? `体素 ${Math.round(Number(voxel) * 100)} cm` : ''
  const ageText = Number.isFinite(ageMs) ? `${(ageMs / 1000).toFixed(1)} 秒前` : ''
  statusLine.value = [pointsText, voxelText, ageText].filter(Boolean).join(' · ') || '地图数据已接入'
}

async function pollPose() {
  if (disposed) return
  try {
    const response = await fetch('/api/field/map/pose', { cache: 'no-store' })
    if (!response.ok) return
    const pose = await response.json()
    if (!disposed && typeof pose?.x === 'number' && typeof pose?.yaw === 'number') placeRobot(pose)
  } catch { /* 位姿轮询失败时保留上一次位置 */ }
}

async function pollPath() {
  if (disposed) return
  const result = await fetchBinary('/api/field/map/path')
  if (result) rebuildPath(result.buf)
}

function renderLoop() {
  if (disposed || !renderer || !controls) return
  controls.update()
  renderer.render(scene!, camera!)
  frameHandle = requestAnimationFrame(renderLoop)
}

onMounted(() => {
  if (!host.value) return
  try {
    scene = new THREE.Scene()
    scene.background = new THREE.Color(0x0d141a)
    const width = host.value.clientWidth || 640
    const height = host.value.clientHeight || 360
    camera = new THREE.PerspectiveCamera(55, width / height, 0.1, 500)
    camera.position.set(6, 8, 8)
    renderer = new THREE.WebGLRenderer({ antialias: true })
    renderer.setSize(width, height)
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
    host.value.appendChild(renderer.domElement)
    controls = new OrbitControls(camera, renderer.domElement)
    // 机器人标记：箭头指向航向
    const arrow = new THREE.ConeGeometry(0.22, 0.55, 12)
    arrow.rotateZ(-Math.PI / 2)
    robotMesh = new THREE.Mesh(arrow, new THREE.MeshBasicMaterial({ color: 0x35c3c8 }))
    robotMesh.position.y = 0.2
    scene.add(robotMesh)
    resizeObserver = new ResizeObserver(() => {
      if (!renderer || !camera || !host.value) return
      const w = host.value.clientWidth || 1
      const h = host.value.clientHeight || 1
      camera.aspect = w / h
      camera.updateProjectionMatrix()
      renderer.setSize(w, h)
    })
    resizeObserver.observe(host.value)
    void pollMap()
    void pollPose()
    void pollPath()
    mapTimer = setInterval(() => { void pollMap() }, 1500)
    poseTimer = setInterval(() => { void pollPose() }, 700)
    pathTimer = setInterval(() => { void pollPath() }, 6000)
    renderLoop()
  } catch {
    statusLine.value = '当前环境不支持 WebGL，无法渲染地图'
  }
})

onBeforeUnmount(() => {
  disposed = true
  if (mapTimer) clearInterval(mapTimer)
  if (poseTimer) clearInterval(poseTimer)
  if (pathTimer) clearInterval(pathTimer)
  if (frameHandle) cancelAnimationFrame(frameHandle)
  resizeObserver?.disconnect()
  pointsMesh?.geometry.dispose()
  pathLine?.geometry.dispose()
  renderer?.dispose()
})
</script>

<template>
  <div class="map-panel">
    <div ref="host" class="map-canvas" role="img" aria-label="建图点云三维视图"></div>
    <div class="map-status"><span>{{ statusLine }}</span><span>拖动旋转 · 滚轮缩放 · FastLIO 实时建图</span></div>
  </div>
</template>

<style scoped>
.map-canvas { width: 100%; height: clamp(240px, 34vh, 430px); border-radius: 6px; overflow: hidden; background: #0d141a; }
.map-canvas :deep(canvas) { display: block; }
.map-status { display: flex; justify-content: space-between; gap: 12px; margin-top: 6px; color: #5f7482; font-size: 12px; }
</style>
