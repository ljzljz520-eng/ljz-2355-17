# 快速开始

本节将介绍如何在项目中使用 My Component Lib。

## 引入组件

在你的 Vue 3 项目中，你可以按需引入或全局引入组件。

### 全局引入

```javascript
import { createApp } from 'vue'
import App from './App.vue'
import MyComponentLib from 'my-component-lib'

const app = createApp(App)
app.use(MyComponentLib)
app.mount('#app')
```

### 基础用法

::: demo 基础按钮用法
examples/button/basic.vue
:::
