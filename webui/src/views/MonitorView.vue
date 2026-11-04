<script setup lang="ts">
import CameraTile from '@/components/CameraTile.vue'
import { useControlStore } from '@/stores/control'
const control = useControlStore()
const cameras = [
  { camera: 'left_wrist', label: '左腕' },
  { camera: 'head', label: '头部' },
  { camera: 'right_wrist', label: '右腕' },
]
</script>

<template>
  <section class="camera-grid" aria-label="三路相机监控">
    <CameraTile v-for="camera in cameras" :key="camera.camera" v-bind="camera"
      :active="control.connected && control.visible" :status="control.cameras[camera.camera]" />
  </section>
</template>
