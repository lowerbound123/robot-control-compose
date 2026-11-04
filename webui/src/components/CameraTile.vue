<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { FontAwesomeIcon } from '@fortawesome/vue-fontawesome'
import { faVideo, faTriangleExclamation } from '@fortawesome/free-solid-svg-icons'
import type { CameraStatus } from '@/stores/control'

const props = defineProps<{ camera: string; label: string; status?: CameraStatus; active: boolean }>()
const version = ref(0)
const broken = ref(false)
let retry: ReturnType<typeof setTimeout> | undefined
const state = computed(() => !props.active ? 'offline' : broken.value ? 'error' : props.status?.state || 'waiting')
const label = computed(() => ({ live: '实时', stale: '画面已过期', waiting: '等待图像', idle: '等待图像', error: '图像异常', offline: '未连接', closed: '已关闭' }[state.value] || '等待图像'))
function reconnect() {
  broken.value = true
  clearTimeout(retry)
  if (props.active) retry = setTimeout(() => { broken.value = false; version.value++ }, 2000)
}
watch(() => props.active, () => { clearTimeout(retry); broken.value = false; version.value++ })
onBeforeUnmount(() => clearTimeout(retry))
</script>

<template>
  <figure class="camera-tile" :data-camera="camera">
    <figcaption class="camera-caption">
      <span class="camera-name">{{ props.label }}</span>
      <span class="camera-state" :data-state="state"><span class="status-dot" />{{ label }}</span>
    </figcaption>
    <div class="camera-frame">
      <img v-if="active" :key="version" :src="`/preview/${camera}.mjpg?connection=${version}`" :alt="`${props.label}实时画面`"
        :data-testid="`camera-${camera}`" width="640" height="480" @error="reconnect" />
      <div v-if="!active || broken || !status || ['waiting', 'idle', 'closed'].includes(state)" class="camera-empty">
        <FontAwesomeIcon :icon="broken ? faTriangleExclamation : faVideo" />
        <span>{{ broken ? '图像连接中断，正在重连' : active ? '等待相机画面' : '等待 RC 连接' }}</span>
      </div>
    </div>
    <div class="camera-meta">
      <span>640 × 480</span>
      <span v-if="state === 'live'">输出 {{ status?.output_fps.toFixed(1) ?? '0.0' }} fps</span>
      <span v-else-if="state === 'stale'">{{ status?.frame_age_s?.toFixed(1) }} 秒前</span>
      <span v-else>— fps</span>
    </div>
    <p v-if="status?.error && active" class="camera-error">{{ status.error }}</p>
  </figure>
</template>
