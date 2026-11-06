<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount } from 'vue'
import { RouterLink, RouterView, useRoute } from 'vue-router'
import { FontAwesomeIcon } from '@fortawesome/vue-fontawesome'
import { faDisplay, faDownload, faPause, faPlay, faRotateLeft } from '@fortawesome/free-solid-svg-icons'
import { Button } from '@/components/ui/button'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import CommandComposer from '@/components/CommandComposer.vue'
import { useControlStore } from '@/stores/control'
import { useCameraStore } from '@/stores/camera'
const route = useRoute()
const control = useControlStore()
const camera = useCameraStore()
const current = computed(() => String(route.meta.section || 'monitor'))
const connection = computed(() => !control.connected ? '未连接' : control.status?.state === 'running' ? '运行中' : '已连接')
onMounted(() => { void control.start(); camera.start() })
onBeforeUnmount(() => { camera.dispose(); control.stop() })
</script>

<template>
  <Tabs :model-value="current" class="app-shell" :class="{ 'has-ai-history': !control.directMode && control.history.length && !control.transcriptCollapsed }">
    <header class="topbar">
      <span class="app-name">Robot Control</span>
      <nav class="top-navigation" aria-label="页面导航">
        <TabsList class="nav-tabs">
          <TabsTrigger value="monitor" as-child class="nav-tab">
            <RouterLink to="/monitor"><FontAwesomeIcon :icon="faDisplay" />监控</RouterLink>
          </TabsTrigger>
        </TabsList>
      </nav>
      <div class="top-actions">
        <span class="connection" :data-connected="control.connected" :title="control.connectionError || connection" :aria-label="connection">
          <span class="status-dot" /><span class="connection-label">{{ connection }}</span>
        </span>
        <Button variant="outline" size="sm" :disabled="!control.canTogglePause"
          :title="control.policyRunning ? '暂停当前策略运行' : '恢复策略运行'" @click="control.togglePause">
          <FontAwesomeIcon :icon="control.policyRunning ? faPause : faPlay" />{{ control.policyRunning ? '暂停' : '恢复' }}
        </Button>
        <Button variant="outline" size="sm" :disabled="!control.logCount" title="导出 AI 运行日志" aria-label="导出 AI 运行日志" @click="control.exportLog">
          <FontAwesomeIcon :icon="faDownload" />导出
        </Button>
        <Button variant="outline" size="sm" :disabled="!control.canReset" title="移动到配置的复位姿态，并保持暂停" @click="control.reset">
          <FontAwesomeIcon :icon="faRotateLeft" />复位
        </Button>
      </div>
    </header>
    <main class="workspace">
      <TabsContent value="monitor" class="monitor-content"><RouterView /></TabsContent>
    </main>
    <CommandComposer />
  </Tabs>
</template>
