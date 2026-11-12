<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as THREE from 'three'
import { OrbitControls } from 'three/addons/controls/OrbitControls.js'
import type { RobotState, RoutePoint } from '../api'

const props = defineProps<{ robot: RobotState | null; points: RoutePoint[]; progress: number }>()
const host = ref<HTMLDivElement>()
const failed = ref(false)
const position = computed(() => {
  const pose = props.robot?.pose ?? props.robot?.position
  const x = pose?.x ?? props.robot?.x
  const y = pose?.y ?? props.robot?.y
  if (typeof x === 'number' && typeof y === 'number') return { x, y }
  if (props.points.length && props.progress > 0) {
    const index = Math.min(props.points.length - 1, Math.max(0, Math.ceil(props.progress / 100 * props.points.length) - 1))
    return props.points[index]!
  }
  return { x: -4.6, y: 0 }
})
const heading = computed(() => {
  const yaw = (props.robot?.pose ?? props.robot?.position)?.yaw
  // Simulator +y maps to scene +z; positive simulator yaw therefore rotates
  // the scene model clockwise about Three's vertical (+y) axis.
  return typeof yaw === 'number' && Number.isFinite(yaw) ? -yaw : 0
})
const targetPosition = new THREE.Vector3()
let renderer: THREE.WebGLRenderer | undefined
let controls: OrbitControls | undefined
let scene: THREE.Scene | undefined
let camera: THREE.PerspectiveCamera | undefined
let robotMesh: THREE.Group | undefined
let routeGroup: THREE.Group | undefined
let frame = 0
let resizeObserver: ResizeObserver | undefined

function rebuildRoute() {
  if (!scene) return
  if (routeGroup) {
    scene.remove(routeGroup)
    routeGroup.traverse(disposeObject)
  }
  routeGroup = new THREE.Group()
  const path = props.points.map(p => new THREE.Vector3(p.x, 0.08, p.y))
  if (path.length > 1) {
    const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints(path), new THREE.LineDashedMaterial({ color: 0x168d91, dashSize: 0.22, gapSize: 0.13 }))
    line.computeLineDistances()
    routeGroup.add(line)
  }
  props.points.forEach((p, i) => {
    const dot = new THREE.Mesh(new THREE.CylinderGeometry(0.17, 0.17, 0.05, 30), new THREE.MeshStandardMaterial({ color: 0x168d91, emissive: 0x0a4548, emissiveIntensity: 0.25 }))
    dot.position.set(p.x, 0.08, p.y)
    routeGroup!.add(dot)
    const sprite = labelSprite(`${String(i + 1).padStart(2, '0')}  ${p.label}`)
    sprite.position.set(p.x, 0.8, p.y)
    routeGroup!.add(sprite)
  })
  scene.add(routeGroup)
}

function labelSprite(text: string): THREE.Sprite {
  const canvas = document.createElement('canvas')
  canvas.width = 512
  canvas.height = 96
  const ctx = canvas.getContext('2d')!
  ctx.fillStyle = 'rgba(255,255,255,0.94)'
  ctx.beginPath()
  ctx.roundRect(0, 0, 512, 80, 18)
  ctx.fill()
  ctx.fillStyle = '#234755'
  ctx.font = '600 35px sans-serif'
  ctx.textAlign = 'center'
  ctx.fillText(text, 256, 51)
  const texture = new THREE.CanvasTexture(canvas)
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: texture, transparent: true, depthTest: false }))
  sprite.scale.set(2.3, 0.43, 1)
  return sprite
}

function disposeObject(object: THREE.Object3D) {
  if (object instanceof THREE.Mesh || object instanceof THREE.Line || object instanceof THREE.Sprite) {
    if ('geometry' in object) object.geometry.dispose()
    const materials = Array.isArray(object.material) ? object.material : [object.material]
    materials.forEach((m: THREE.Material) => {
      if ('map' in m && m.map instanceof THREE.Texture) m.map.dispose()
      m.dispose()
    })
  }
}

onMounted(() => {
  if (!host.value) return
  try {
    scene = new THREE.Scene()
    scene.background = new THREE.Color(0xf2f6f8)
    scene.fog = new THREE.Fog(0xf2f6f8, 20, 45)
    camera = new THREE.PerspectiveCamera(40, 1, 0.1, 100)
    camera.position.set(12, 12, 14)
    renderer = new THREE.WebGLRenderer({ antialias: true })
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
    renderer.shadowMap.enabled = true
    renderer.shadowMap.type = THREE.PCFSoftShadowMap
    host.value.appendChild(renderer.domElement)
    controls = new OrbitControls(camera, renderer.domElement)
    controls.target.set(0, 0.5, 0)
    controls.enableDamping = true
    controls.minDistance = 8
    controls.maxDistance = 30
    controls.maxPolarAngle = Math.PI / 2.15
    scene.add(new THREE.HemisphereLight(0xffffff, 0x77868f, 2.6))
    const light = new THREE.DirectionalLight(0xffffff, 3)
    light.position.set(4, 12, 6)
    light.castShadow = true
    light.shadow.mapSize.set(2048, 2048)
    light.shadow.camera.left = -9
    light.shadow.camera.right = 9
    light.shadow.camera.top = 9
    light.shadow.camera.bottom = -9
    scene.add(light)
    const ground = new THREE.Mesh(new THREE.BoxGeometry(12.5, 0.18, 10), new THREE.MeshStandardMaterial({ color: 0xe1e9ed, roughness: 0.92 }))
    ground.position.y = -0.12
    ground.receiveShadow = true
    scene.add(ground)
    const grid = new THREE.GridHelper(12, 24, 0xc3d1d9, 0xd4dfe5)
    grid.position.y = -0.015
    scene.add(grid)
    for (const z of [-3, 3]) {
      for (const x of [-3, 0, 3]) {
        const rack = new THREE.Group()
        const body = new THREE.Mesh(new THREE.BoxGeometry(1.3, 2.5, 1.05), new THREE.MeshStandardMaterial({ color: 0x344758, metalness: 0.25, roughness: 0.65 }))
        body.position.y = 1.25
        body.castShadow = true
        rack.add(body)
        for (let n = 0; n < 9; n++) {
          const server = new THREE.Mesh(new THREE.BoxGeometry(1.11, 0.17, 0.022), new THREE.MeshStandardMaterial({ color: n % 3 === 0 ? 0x56707c : 0x465c6b }))
          server.position.set(0, 0.28 + n * 0.23, z < 0 ? 0.54 : -0.54)
          rack.add(server)
          const led = new THREE.Mesh(new THREE.BoxGeometry(0.055, 0.035, 0.03), new THREE.MeshBasicMaterial({ color: 0x5ee0ba }))
          led.position.set(-0.38, server.position.y, server.position.z * 1.04)
          rack.add(led)
        }
        rack.position.set(x, 0, z)
        scene.add(rack)
      }
    }
    robotMesh = new THREE.Group()
    const robotMaterial = new THREE.MeshStandardMaterial({ color: 0x19a3a7, metalness: 0.15, roughness: 0.5 })
    const body = new THREE.Mesh(new THREE.CapsuleGeometry(0.22, 0.36, 8, 16), robotMaterial)
    body.position.y = 0.85
    body.castShadow = true
    robotMesh.add(body)
    const head = new THREE.Mesh(new THREE.SphereGeometry(0.17, 16, 16), new THREE.MeshStandardMaterial({ color: 0xe9f5f5 }))
    head.position.y = 1.39
    robotMesh.add(head)
    const visor = new THREE.Mesh(new THREE.BoxGeometry(0.04, 0.09, 0.21), new THREE.MeshStandardMaterial({ color: 0x265466 }))
    visor.position.set(0.15, 1.42, 0)
    robotMesh.add(visor)
    for (const x of [-0.13, 0.13]) {
      const leg = new THREE.Mesh(new THREE.CapsuleGeometry(0.075, 0.3, 4, 8), robotMaterial)
      leg.position.set(x, 0.3, 0)
      robotMesh.add(leg)
    }
    const halo = new THREE.Mesh(new THREE.RingGeometry(0.4, 0.48, 40), new THREE.MeshBasicMaterial({ color: 0x199b9e, side: THREE.DoubleSide, transparent: true, opacity: 0.55 }))
    halo.rotation.x = -Math.PI / 2
    halo.position.y = 0.04
    robotMesh.add(halo)
    const facingArrow = new THREE.ArrowHelper(new THREE.Vector3(1, 0, 0), new THREE.Vector3(0.27, 0.13, 0), 0.56, 0x168d91, 0.2, 0.18)
    robotMesh.add(facingArrow)
    scene.add(robotMesh)
    rebuildRoute()
    resizeObserver = new ResizeObserver(() => {
      if (!host.value || !renderer || !camera) return
      const { width, height } = host.value.getBoundingClientRect()
      camera.aspect = width / Math.max(height, 1)
      camera.updateProjectionMatrix()
      renderer.setSize(width, height)
    })
    resizeObserver.observe(host.value)
    const animate = () => {
      frame = requestAnimationFrame(animate)
      if (!renderer || !camera || !scene || !robotMesh) return
      robotMesh.position.lerp(targetPosition.set(position.value.x, 0, position.value.y), 0.16)
      const turn = heading.value - robotMesh.rotation.y
      robotMesh.rotation.y += Math.atan2(Math.sin(turn), Math.cos(turn)) * 0.16
      controls?.update()
      renderer.render(scene, camera)
    }
    animate()
  } catch {
    failed.value = true
  }
})

watch(() => props.points, rebuildRoute, { deep: true })
onBeforeUnmount(() => {
  cancelAnimationFrame(frame)
  resizeObserver?.disconnect()
  controls?.dispose()
  scene?.traverse(disposeObject)
  renderer?.dispose()
})
</script>

<template>
  <div ref="host" class="room-scene" data-testid="room-scene">
    <div v-if="failed" class="scene-fallback">当前浏览器未提供WebGL渲染能力，请检查浏览器图形加速。任务操作仍可使用。</div>
    <div class="scene-label"><span class="mini-dot"></span> 数据中心 · 模拟场景 <span class="scene-divider">/</span> 6 个机柜</div>
    <div class="scene-legend"><span><i class="legend-rack"></i> 机柜模型</span><span><i class="legend-route"></i> 巡检路线</span><span><i class="legend-robot"></i> 模拟机器人</span></div>
    <div class="scene-help">拖动旋转 · 滚轮缩放</div>
  </div>
</template>
