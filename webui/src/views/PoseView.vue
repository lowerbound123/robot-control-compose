<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import PoseChart from '@/components/PoseChart.vue'
import { usePoseStore } from '@/stores/pose'
import type { PoseMode } from '@/lib/pose/window'
const route = useRoute()
const pose = usePoseStore()
const mode = computed(() => route.meta.section as PoseMode)
</script>

<template>
  <section class="pose-page" :data-samples="pose.status.count" :data-connected="pose.status.connected" :aria-label="mode === 'joints' ? '关节位姿' : '末端位姿'">
    <div class="pose-intro">
      <span>实际反馈 · 每图 60 秒 · 滚轮回看本次会话</span>
      <span v-if="pose.status.error" class="pose-error">{{ pose.status.error }}<template v-if="pose.status.values"> · 显示最后有效值</template></span>
      <span v-else>{{ pose.status.count ? '实时接收中' : '等待机器人反馈' }}</span>
    </div>
    <section v-for="(arm, index) in ['左臂', '右臂']" :key="`${mode}-${arm}`" class="pose-arm" :aria-label="arm">
      <h2>{{ arm }}</h2>
      <div class="pose-grid">
        <PoseChart v-for="channel in 7" :key="`${mode}-${index * 7 + channel - 1}`" :mode="mode" :channel="index * 7 + channel - 1" />
      </div>
    </section>
  </section>
</template>
