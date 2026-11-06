import { createRouter, createWebHistory } from 'vue-router'
import MonitorView from '@/views/MonitorView.vue'
import PoseView from '@/views/PoseView.vue'
export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/monitor' },
    { path: '/monitor', meta: { section: 'monitor' }, children: [
      { path: '', name: 'monitor', component: MonitorView },
      { path: ':camera(left_wrist|head|right_wrist)', name: 'monitor-camera', component: MonitorView },
    ] },
    { path: '/joints', name: 'joints', component: PoseView, meta: { section: 'joints' } },
    { path: '/eef', name: 'eef', component: PoseView, meta: { section: 'eef' } },
    { path: '/:pathMatch(.*)*', redirect: '/monitor' },
  ],
})
router.afterEach(to => {
  const name = to.meta.section === 'joints' ? '关节位姿' : to.meta.section === 'eef' ? '末端位姿' : '监控'
  document.title = `Robot Control · ${name}`
})
