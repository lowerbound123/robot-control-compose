<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount } from 'vue'
import { RouterLink, RouterView, useRoute } from 'vue-router'
import { FontAwesomeIcon } from '@fortawesome/vue-fontawesome'
import { faDisplay, faRotateLeft } from '@fortawesome/free-solid-svg-icons'
import { Button } from '@/components/ui/button'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import CommandComposer from '@/components/CommandComposer.vue'
import { useControlStore } from '@/stores/control'
const route = useRoute()
const control = useControlStore()
const current = computed(() => String(route.name || 'monitor'))
const connection = computed(() => !control.connected ? '未连接' : control.status?.state === 'running' ? '运行中' : '已连接')
onMounted(() => void control.start())
onBeforeUnmount(control.stop)
</script>

<template>
  <Tabs :model-value="current" class="app-shell">
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
        <span class="connection" :data-connected="control.connected" :title="control.connectionError">
          <span class="status-dot" />{{ connection }}
        </span>
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
