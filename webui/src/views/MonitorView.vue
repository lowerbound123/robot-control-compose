<script setup lang="ts">
import CameraTile from '@/components/CameraTile.vue'
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { cameras } from '@/lib/camera/types'
import { useControlStore } from '@/stores/control'
const control = useControlStore()
const route = useRoute()
const selected = computed(() => cameras.find(camera => camera.camera === route.params.camera))
const shown = computed(() => selected.value ? [selected.value] : cameras)
</script>

<template>
  <section class="camera-grid" :class="{ 'camera-single': selected }" :aria-label="selected ? `${selected.label}相机监控` : '三路相机监控'">
    <CameraTile v-for="camera in shown" :key="camera.camera" v-bind="camera" :enlarged="!!selected"
      :active="control.connected" :status="control.cameras[camera.camera]" />
  </section>
</template>
