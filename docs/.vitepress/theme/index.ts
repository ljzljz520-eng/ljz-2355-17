import DefaultTheme from 'vitepress/theme'
import VpDemo from './components/VpDemo.vue'
import VpApi from './components/VpApi.vue'
import BaseButton from './components/BaseButton.vue'
import './custom.css'

export default {
  extends: DefaultTheme,
  enhanceApp({ app }) {
    app.component('VpDemo', VpDemo)
    app.component('VpApi', VpApi)
    app.component('BaseButton', BaseButton)
    
    // Auto register examples
    const examples = import.meta.glob('../../examples/**/*.vue', { eager: true })
    for (const path in examples) {
      const name = path
        .replace('../../examples/', 'demo-')
        .replace(/\//g, '-')
        .replace('.vue', '')
      app.component(name, (examples[path] as any).default)
    }
  }
}
