import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { config } from '@fortawesome/fontawesome-svg-core'
import '@fortawesome/fontawesome-svg-core/styles.css'
import App from './App.vue'
import { router } from './router'
import './style.css'
import './glass.css'
config.autoAddCss = false
createApp(App).use(createPinia()).use(router).mount('#app')
