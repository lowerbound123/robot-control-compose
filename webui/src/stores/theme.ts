import { ref, shallowRef } from 'vue'
import { defineStore } from 'pinia'
import type { PlotColors } from '@/lib/pose/draw'

type Theme = 'light' | 'dark'
const STORAGE_KEY = 'rc-webui-theme'
const normalize = (value: string | null | undefined): Theme => value === 'dark' ? 'dark' : 'light'
function plotColors(): PlotColors {
  const style = getComputedStyle(document.documentElement)
  return { grid: style.getPropertyValue('--plot-grid').trim(), axis: style.getPropertyValue('--plot-axis').trim(),
    line: style.getPropertyValue('--plot-line').trim(), reference: style.getPropertyValue('--plot-reference').trim() }
}

export const useThemeStore = defineStore('theme', () => {
  const current = ref<Theme>(normalize(document.documentElement.dataset.theme))
  const colors = shallowRef(plotColors())
  function apply(value: string | null | undefined) {
    current.value = normalize(value)
    document.documentElement.dataset.theme = current.value
    document.querySelector('meta[name="color-scheme"]')?.setAttribute('content', current.value)
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content',
      getComputedStyle(document.documentElement).getPropertyValue('--page-background').trim())
    // Resolve CSS colors only on theme changes, never in the high-frequency draw loop.
    colors.value = plotColors()
  }
  function toggle() {
    apply(current.value === 'dark' ? 'light' : 'dark')
    try { localStorage.setItem(STORAGE_KEY, current.value) } catch { /* In-memory switching still works. */ }
  }
  function storage(event: StorageEvent) {
    try { if (event.storageArea !== localStorage) return } catch { return }
    if (event.key === STORAGE_KEY || event.key === null) apply(event.newValue)
  }
  function start() { apply(document.documentElement.dataset.theme); window.addEventListener('storage', storage) }
  function stop() { window.removeEventListener('storage', storage) }
  return { current, colors, toggle, start, stop }
})
