import { createRouter, createWebHistory } from 'vue-router'
import MonitorView from '@/views/MonitorView.vue'
export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/monitor' },
    { path: '/monitor', meta: { section: 'monitor' }, children: [
      { path: '', name: 'monitor', component: MonitorView },
      { path: ':camera(left_wrist|head|right_wrist)', name: 'monitor-camera', component: MonitorView },
    ] },
    { path: '/:pathMatch(.*)*', redirect: '/monitor' },
  ],
})
