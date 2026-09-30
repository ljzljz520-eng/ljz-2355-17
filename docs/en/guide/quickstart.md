# Quick Start

This section will introduce how to use My Component Lib in your project.

## Import Components

In your Vue 3 project, you can import components on demand or globally.

### Global Import

```javascript
import { createApp } from 'vue'
import App from './App.vue'
import MyComponentLib from 'my-component-lib'

const app = createApp(App)
app.use(MyComponentLib)
app.mount('#app')
```

### Basic Usage

::: demo Basic Button Usage
examples/button/basic.vue
:::