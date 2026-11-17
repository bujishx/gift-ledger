import { createApp } from 'vue'
import App from './App.vue'
import './style.css'
import './ui.css'
import './modalFamily.css'
import './entryRefinements.css'
import './theme.css'
import { initializeTheme } from './lib/theme.js'

initializeTheme()
createApp(App).mount('#app')
