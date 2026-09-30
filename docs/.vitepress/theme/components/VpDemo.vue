<template>
  <div class="vp-demo-wrapper">
    <div class="vp-demo-preview">
      <slot></slot>
    </div>
    
    <div v-if="$slots.description" class="vp-demo-description">
      <slot name="description"></slot>
    </div>

    <div v-show="isExpanded" class="vp-demo-source">
      <slot name="source"></slot>
    </div>
    
    <div class="vp-demo-footer">
      <div class="footer-actions">
        <span class="action-item" @click="copyCode" :class="{ copied: isCopied }">
          {{ copyText }}
        </span>
        <span class="action-item" @click="toggleSource">
          {{ expandText }}
        </span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import { useData } from 'vitepress'

const isExpanded = ref(false)
const isCopied = ref(false)
const { lang } = useData()

const toggleSource = () => {
  isExpanded.value = !isExpanded.value
}

const copyText = computed(() => {
  if (isCopied.value) return lang.value === 'zh-CN' ? '已复制!' : 'Copied!'
  return lang.value === 'zh-CN' ? '复制代码' : 'Copy Code'
})

const expandText = computed(() => {
  if (isExpanded.value) return lang.value === 'zh-CN' ? '隐藏代码' : 'Hide Code'
  return lang.value === 'zh-CN' ? '查看代码' : 'View Code'
})

const copyCode = async () => {
  const wrapper = document.querySelector('.vp-demo-source')
  if (!wrapper) return
  
  const code = wrapper.textContent
  
  if (code) {
    try {
      await navigator.clipboard.writeText(code)
      isCopied.value = true
      setTimeout(() => {
        isCopied.value = false
      }, 2000)
    } catch (err) {
      console.error('Failed to copy:', err)
    }
  }
}
</script>

<style scoped>
.vp-demo-wrapper {
  border: 1px solid var(--vp-c-gutter);
  border-radius: 4px;
  margin: 16px 0;
  overflow: hidden;
  background-color: var(--vp-c-bg);
}

.vp-demo-preview {
  padding: 24px;
  background-color: var(--vp-c-bg);
}

.vp-demo-description {
  padding: 10px 20px;
  margin: 10px;
  border: 1px solid var(--vp-c-gutter);
  background-color: var(--vp-c-bg-soft);
  font-size: 14px;
  border-radius: 4px;
}

.vp-demo-source {
  background-color: var(--vp-c-bg-alt);
  border-top: 1px solid var(--vp-c-gutter);
}

.vp-demo-source :deep(div[class*='language-']) {
  margin: 0 !important;
  border-radius: 0;
}

.vp-demo-footer {
  height: 44px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-top: 1px solid var(--vp-c-gutter);
  background-color: var(--vp-c-bg-soft);
}

.footer-actions {
  display: flex;
  gap: 20px;
  font-size: 14px;
  color: var(--vp-c-text-2);
}

.action-item {
  cursor: pointer;
  transition: color 0.2s;
  user-select: none;
  display: flex;
  align-items: center;
}

.action-item:hover {
  color: var(--vp-c-brand);
}

.action-item.copied {
  color: var(--vp-c-green-1);
}
</style>
