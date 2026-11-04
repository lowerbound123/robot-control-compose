import { createRouter, createWebHistory } from 'vue-router'
import MonitorView from '@/views/MonitorView.vue'
export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/monitor' },
    { path: '/monitor', name: 'monitor', component: MonitorView },
    { path: '/:pathMatch(.*)*', redirect: '/monitor' },
  ],
})
