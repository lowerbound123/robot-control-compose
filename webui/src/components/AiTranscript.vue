<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { FontAwesomeIcon } from '@fortawesome/vue-fontawesome'
import { faArrowDown, faCheck, faCircleExclamation, faStop } from '@fortawesome/free-solid-svg-icons'
import type { AiEntry } from '@/lib/command-presentation'
const props = defineProps<{ entries: AiEntry[] }>()
const collapsed = defineModel<boolean>('collapsed', { default: false })
const log = ref<HTMLElement>()
const following = ref(true)
let observer: ResizeObserver | undefined
let viewportWidth = 0, viewportHeight = 0
function measure() {
  viewportWidth = log.value?.clientWidth ?? 0
  viewportHeight = log.value?.clientHeight ?? 0
}
function scrollToLatest() {
  following.value = true
  if (log.value) log.value.scrollTop = log.value.scrollHeight
}
function onScroll() {
  const element = log.value
  if (!element) return
  // Reflow can fire scroll events without reader input. Preserve their follow preference.
  if (element.clientWidth !== viewportWidth || element.clientHeight !== viewportHeight) {
    measure()
    if (following.value) scrollToLatest()
    return
  }
  following.value = element.scrollHeight - element.clientHeight - element.scrollTop < 32
}
watch(collapsed, async value => { if (!value) { await nextTick(); measure(); if (following.value) scrollToLatest() } })
watch(() => props.entries.map(entry => `${entry.id}:${entry.rounds.length}:${entry.reasoning.length}:${entry.message.length}:${entry.status}`).join('|'), async () => {
  if (!following.value) return
  await nextTick()
  if (following.value) scrollToLatest()
}, { flush: 'post' })
onMounted(() => {
  measure()
  scrollToLatest()
  observer = new ResizeObserver(() => { measure(); if (following.value) scrollToLatest() })
  if (log.value) observer.observe(log.value)
})
onBeforeUnmount(() => observer?.disconnect())
</script>

<template>
  <section class="ai-transcript" :class="{ 'is-collapsed': collapsed }">
    <button class="transcript-toggle" type="button" :aria-label="collapsed ? '展开 AI 日志' : '收起 AI 日志'"
      :aria-expanded="!collapsed" aria-controls="ai-transcript-body" @click="collapsed = !collapsed">
      <svg viewBox="0 0 20 14" aria-hidden="true"><path d="M3 3 Q1 1 4 1 H16 Q19 1 17 3 L12 11 Q10 14 8 11 Z" /></svg>
    </button>
    <div v-show="!collapsed" id="ai-transcript-body" class="transcript-body">
      <div class="transcript-glass" aria-hidden="true" />
      <div ref="log" class="transcript-scroll" role="log" aria-label="AI 输出历史" aria-live="off" tabindex="0" @scroll.passive="onScroll">
        <article v-for="entry in entries" :key="entry.id" class="ai-entry" :data-outcome="entry.outcome">
          <p class="ai-request"><span class="ai-speaker">你</span>{{ entry.input }}</p>
          <template v-if="entry.rounds.length">
            <section v-for="round in entry.rounds" :key="round.id" class="ai-round">
              <div v-if="round.reasoning" class="ai-reasoning">
                <span class="ai-caption">思考 · {{ round.id }}</span>
                <p>{{ round.reasoning }}</p>
              </div>
              <p v-if="round.message && (round.tool || !entry.finalAnswer)" class="ai-answer">{{ round.message }}</p>
              <p v-if="round.tool" class="ai-tool-summary">
                {{ round.tool.name === 'policy_command' ? `${round.tool.arguments.instance_id} · ${round.tool.arguments.command}` : round.tool.name }}
                <span>{{ round.status === 'succeeded' ? '已确认' : round.status === 'failed' ? '失败' : round.status === 'cancelled' ? '已取消' : '执行中' }}</span>
              </p>
            </section>
          </template>
          <div v-else-if="entry.reasoning" class="ai-reasoning">
            <span class="ai-caption">思考</span>
            <p>{{ entry.reasoning }}</p>
          </div>
          <p v-if="entry.finalAnswer || (!entry.rounds.length && entry.message)" class="ai-answer">{{ entry.finalAnswer || entry.message }}</p>
          <p v-if="entry.outcome !== 'running' && entry.status" class="ai-outcome">
            <FontAwesomeIcon v-if="entry.outcome === 'success'" :icon="faCheck" />
            <FontAwesomeIcon v-else-if="entry.outcome === 'error' || entry.outcome === 'pending'" :icon="faCircleExclamation" />
            <FontAwesomeIcon v-else-if="entry.outcome === 'cancelled'" :icon="faStop" />
            {{ entry.status }}
          </p>
        </article>
      </div>
      <button v-if="!following" class="latest-button" type="button" @click="scrollToLatest">
        <FontAwesomeIcon :icon="faArrowDown" />回到最新
      </button>
    </div>
  </section>
</template>
