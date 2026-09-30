<template>
  <button class="base-button" :class="type">
    <slot></slot>
  </button>
</template>

<script setup>
defineProps({
  type: {
    type: String,
    default: 'default'
  }
})
</script>

<style scoped>
.base-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 8px 15px;
  border-radius: 4px;
  border: 1px solid var(--vp-c-gutter);
  background: var(--vp-c-bg);
  color: var(--vp-c-text-1);
  cursor: pointer;
  outline: none;
  transition: .2s;
  font-size: 14px;
}

.base-button:hover {
  color: var(--vp-c-brand);
  border-color: var(--vp-c-brand-light);
  background-color: var(--vp-c-brand-soft);
}

.base-button.primary {
  background-color: var(--vp-c-brand);
  border-color: var(--vp-c-brand);
  color: #fff;
}

.base-button.primary:hover {
  background-color: var(--vp-c-brand-light);
  border-color: var(--vp-c-brand-light);
}
</style>