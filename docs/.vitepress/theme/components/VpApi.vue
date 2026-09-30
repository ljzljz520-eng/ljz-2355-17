<template>
  <div class="vp-api-table">
    <h3 v-if="title">{{ title }}</h3>
    <table>
      <thead>
        <tr>
          <th v-for="header in headers" :key="header">{{ header }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="prop in props" :key="prop.name">
          <td><code>{{ prop.name }}</code></td>
          <td>{{ prop.description }}</td>
          <td><code>{{ prop.type }}</code></td>
          <td><code>{{ prop.default }}</code></td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useData } from 'vitepress'

defineProps({
  title: { type: String, default: '' },
  props: { type: Array, default: () => [] }
})

const { lang } = useData()

const headers = computed(() => {
  return lang.value === 'zh-CN' 
    ? ['属性名', '说明', '类型', '默认值']
    : ['Name', 'Description', 'Type', 'Default']
})
</script>

<style scoped>
.vp-api-table {
  width: 100%;
  overflow-x: auto;
  margin: 20px 0;
}
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 14px;
  min-width: 600px; /* Ensure table doesn't squash on mobile */
}
th, td {
  text-align: left;
  padding: 12px;
  border-bottom: 1px solid var(--vp-c-gutter);
}
th {
  background-color: var(--vp-c-bg-soft);
  font-weight: 600;
  color: var(--vp-c-text-1);
  white-space: nowrap;
}
td {
  color: var(--vp-c-text-2);
}
code {
  color: var(--vp-c-brand);
  background-color: var(--vp-c-brand-soft);
  padding: 2px 4px;
  border-radius: 4px;
  font-family: var(--vp-font-family-mono);
}
</style>