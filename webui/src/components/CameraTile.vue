<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { FontAwesomeIcon } from '@fortawesome/vue-fontawesome'
import { faDownload, faSpinner, faVideo } from '@fortawesome/free-solid-svg-icons'
import { Button } from '@/components/ui/button'
import { useCameraStore } from '@/stores/camera'
import type { CameraId } from '@/lib/camera/types'
import type { CameraStatus } from '@/stores/control'

const props = defineProps<{ camera: CameraId; label: string; status?: CameraStatus; active: boolean; enlarged?: boolean }>()
const stream = useCameraStore(), canvas = ref<HTMLCanvasElement>()
const recording = computed(() => stream.state[props.camera])
const state = computed(() => !props.active ? 'offline' : recording.value.streamError ? 'error' : props.status?.state || 'waiting')
const stateLabel = computed(() => ({ live: '实时', stale: '画面已过期', waiting: '等待图像', idle: '等待图像', error: '图像异常', offline: '未连接', closed: '已关闭' }[state.value] || '等待图像'))
const duration = computed(() => {
  const seconds = Math.floor(recording.value.frames / 30)
  return `${Math.floor(seconds / 60).toString().padStart(2, '0')}:${(seconds % 60).toString().padStart(2, '0')}`
})
onMounted(() => stream.bind(props.camera, canvas.value!))
onBeforeUnmount(() => stream.bind(props.camera, null))
</script>

<template>
  <figure class="camera-tile" :data-camera="camera" :data-recorded-frames="recording.frames"
    :data-pending-frames="recording.pendingFrames" :data-recorded-bytes="recording.bytes">
    <figcaption class="camera-caption">
      <span class="camera-name">{{ label }}</span>
      <span class="camera-state" :data-state="state"><span class="status-dot" />{{ stateLabel }}</span>
    </figcaption>
    <div class="camera-frame">
      <canvas ref="canvas" width="640" height="480" :aria-label="`${label}实时画面`" role="img"
        :data-testid="`camera-${camera}`" :data-ready="recording.ready" :data-sequence="recording.sequence" />
      <div v-if="!recording.ready" class="camera-empty">
        <FontAwesomeIcon :icon="faVideo" /><span>{{ active ? '等待相机画面' : '等待 RC 连接' }}</span>
      </div>
      <RouterLink v-if="!enlarged" class="camera-open" :to="`/monitor/${camera}`" :aria-label="`放大${label}画面`" :title="`放大${label}画面`" />
    </div>
    <div class="camera-meta">
      <span>640 × 480</span>
      <span v-if="state === 'live'">输出 {{ status?.output_fps.toFixed(1) ?? '0.0' }} fps</span>
      <span v-else-if="state === 'stale'">{{ status?.frame_age_s?.toFixed(1) }} 秒前</span>
      <span v-else>— fps</span>
    </div>
    <div class="camera-export-row">
      <span class="recording-meta">{{ duration }} <span aria-hidden="true">·</span> 30 fps</span>
      <Button variant="outline" size="sm" :disabled="!recording.frames || recording.exporting" :aria-label="`导出${label}视频`"
        :title="`下载本次网页会话的${label}视频`" @click="stream.download(camera)">
        <FontAwesomeIcon :icon="recording.exporting ? faSpinner : faDownload" :spin="recording.exporting" />{{ recording.exporting ? '导出中' : '导出' }}
      </Button>
    </div>
    <p v-if="recording.downloadError || recording.error" class="camera-error" aria-live="polite">{{ recording.downloadError || recording.error }}</p>
    <p v-if="recording.streamError || status?.error" class="camera-error">{{ recording.streamError || status?.error }}</p>
  </figure>
</template>
