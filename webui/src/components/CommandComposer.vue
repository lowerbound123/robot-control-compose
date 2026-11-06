<script setup lang="ts">
import { ref } from 'vue'
import { FontAwesomeIcon } from '@fortawesome/vue-fontawesome'
import { faArrowUp, faStop, faChevronDown, faCircleCheck, faCircleExclamation, faSpinner } from '@fortawesome/free-solid-svg-icons'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { useControlStore } from '@/stores/control'
import AiTranscript from './AiTranscript.vue'
const control = useControlStore()
const input = ref('')
const expanded = ref(false)
function submit() {
  if (input.value.trim() && !control.busy) {
    expanded.value = false
    void control.submit(input.value)
  }
}
function keydown(event: KeyboardEvent) {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); submit() }
}
</script>

<template>
  <footer class="composer-area">
    <div class="composer-wrap">
      <div class="composer-feedback">
        <AiTranscript v-if="!control.directMode && control.history.length" v-show="!expanded" :entries="control.history"
          v-model:collapsed="control.transcriptCollapsed" />
        <div v-if="control.message" class="execution-status" :data-error="control.failed" role="status" aria-live="polite">
          <FontAwesomeIcon :icon="control.busy ? faSpinner : control.showRecovery || control.failed ? faCircleExclamation : control.phase === 'cancelled' ? faStop : faCircleCheck" :spin="control.busy" />
          <span>{{ control.message }}</span>
          <Button v-if="control.details.length" variant="ghost" size="icon-sm" :aria-expanded="expanded" aria-label="展开操作回执" @click="expanded = !expanded">
            <FontAwesomeIcon :icon="faChevronDown" :class="{ 'rotate-180': expanded }" />
          </Button>
        </div>
        <div v-if="expanded && control.details.length" class="receipt-list" aria-label="操作回执">
          <pre v-for="(detail, index) in control.details" :key="index">{{ JSON.stringify(detail, null, 2) }}</pre>
        </div>
        <div v-if="control.showRecovery" class="recovery-row">
          <span>上一条操作需要确认</span>
          <Button v-if="control.unresolved" variant="outline" size="sm" :disabled="control.busy" @click="control.reconcile">查询原回执</Button>
          <Button v-if="control.uncertain" variant="outline" size="sm" :disabled="control.busy" @click="control.acknowledgeUnknown">已在 RC 核对，解除锁定</Button>
        </div>
      </div>
      <div class="composer-shell" :data-phase="control.phase">
        <div class="composer-halo" aria-hidden="true">
          <span class="halo-spectrum" /><span class="halo-amber" /><span class="halo-green" /><span class="halo-red" />
        </div>
        <form class="composer" @submit.prevent="submit">
          <Textarea v-model="input" :aria-label="control.directMode ? '命令输入' : '自然语言指令'"
            :placeholder="control.directMode ? '每行一条 TUI 命令，例如 status 或 pause' : '输入指令，例如「开始录制」或「复位」'"
            :maxlength="4000" rows="1" class="command-input" @keydown="keydown" />
          <Button v-if="control.busy" type="button" size="icon" class="send-button" aria-label="停止后续步骤" title="停止等待与后续步骤，已受理操作以 RC 回执为准" @click="control.cancel">
            <FontAwesomeIcon :icon="faStop" />
          </Button>
          <Button v-else type="submit" size="icon" class="send-button" aria-label="发送指令" :disabled="!input.trim() || !control.connected || !control.inputReady">
            <FontAwesomeIcon :icon="faArrowUp" />
          </Button>
        </form>
      </div>
      <div class="composer-hint">
        <span>Enter 发送 <span class="hint-divider">·</span> Shift + Enter 换行</span>
        <span :title="control.configError">{{ control.directMode ? '命令输入 · 每行一条 TUI 命令' : control.apiReady ? '自然语言控制' : 'AI 未配置' }}</span>
      </div>
      <p v-if="control.configError" class="config-note">{{ control.configError }}</p>
      <p v-if="control.logError" class="config-note log-error" aria-live="polite">{{ control.logError }}</p>
    </div>
  </footer>
</template>
