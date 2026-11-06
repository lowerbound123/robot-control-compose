<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { usePoseStore } from '@/stores/pose'
import { useThemeStore } from '@/stores/theme'
import { drawChart } from '@/lib/pose/draw'
import { channels, type PoseMode } from '@/lib/pose/window'
const props = defineProps<{ mode: PoseMode; channel: number }>()
const pose = usePoseStore()
const theme = useThemeStore()
const canvas = ref<HTMLCanvasElement | null>(null)
const revision = ref(0)
const viewport = pose.windowFor(props.mode, props.channel)
const spec = channels(props.mode)[props.channel % 7]
const side = props.channel < 7 ? 'left' : 'right'
const arm = side === 'left' ? '左臂' : '右臂'
const current = computed(() => pose.status.values?.[props.mode][props.channel])
const outside = computed(() => current.value !== undefined && (current.value < spec.range[0] || current.value > spec.range[1]))
const bounds = computed(() => { revision.value; return viewport.range(pose.status.first ?? 0, pose.status.latest ?? 0) })
const following = computed(() => { revision.value; return viewport.following })
let observer: IntersectionObserver | undefined, visible = true
function live() { viewport.live(); revision.value++ }
function pan(delta: number) {
  if (pose.status.first === null || pose.status.latest === null) return false
  const previous = bounds.value[0]
  viewport.pan(delta, pose.status.first, pose.status.latest); revision.value++
  return previous !== bounds.value[0]
}
function wheel(event: WheelEvent) {
  if (event.ctrlKey || event.metaKey) return
  const delta = Math.abs(event.deltaX) > Math.abs(event.deltaY) ? event.deltaX : event.deltaY
  const pixels = delta * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? 300 : 1)
  if (pan(pixels * 50)) event.preventDefault()
}
function keyboard(event: KeyboardEvent) {
  if (event.key === 'End') { live(); event.preventDefault() }
  else if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
    pan(event.key === 'ArrowLeft' ? -5000 : 5000); event.preventDefault()
  } else if (event.key === 'Home') { pan(-Infinity); event.preventDefault() }
}
onMounted(() => {
  canvas.value!.addEventListener('wheel', wheel, { passive: false })
  observer = new IntersectionObserver(entries => { visible = entries[0].isIntersecting })
  observer.observe(canvas.value!)
  pose.bind(props.mode, props.channel, {
    request: () => !visible || pose.status.first === null ? null : ({ key: pose.key(props.mode, props.channel),
      mode: props.mode, channel: props.channel, start: bounds.value[0], end: bounds.value[1], pixels: canvas.value?.clientWidth ?? 180 }),
    draw: chart => { if (canvas.value) drawChart(canvas.value, chart, spec, current.value, theme.colors) },
  })
})
onBeforeUnmount(() => {
  canvas.value?.removeEventListener('wheel', wheel); observer?.disconnect()
  pose.bind(props.mode, props.channel, null)
})
</script>

<template>
  <figure class="pose-chart" :data-testid="`${mode}-${side}-${channel % 7}`" :data-following="following" :data-start="bounds[0]">
    <figcaption class="pose-caption">
      <span class="pose-name">{{ spec.label }} <small>{{ spec.unit }}</small></span>
      <span class="pose-value" :class="{ 'pose-outside': outside }" :title="outside ? '当前值超出固定纵轴范围' : '最新有效反馈值'">
        {{ current === undefined ? '—' : current.toFixed(3) }}<span v-if="outside"> ↗</span>
      </span>
    </figcaption>
    <div class="pose-plot">
      <canvas ref="canvas" tabindex="0" role="img" :aria-label="`${arm} ${spec.label} 时间曲线，滚轮或左右方向键回看，End 回到实时`" @keydown="keyboard" />
      <span v-if="current === undefined" class="pose-empty">等待反馈</span>
    </div>
    <div class="pose-chart-footer">
      <span :class="{ 'pose-outside': outside }">{{ outside ? '超出范围' : following ? '实时 · 60 秒' : '历史 · 60 秒' }}</span>
      <button type="button" :disabled="following" @click="live">回到实时</button>
    </div>
  </figure>
</template>
