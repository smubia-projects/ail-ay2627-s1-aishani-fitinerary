<script setup>
// Shown when the shared rate-limit engine answers 503 demo_paused (an operator paused the demo via the kill switch).
// Distinct from the Rate-Limit CTA (429) and from the solver's own 503 solver_busy notice (BusyNotice.vue).
import { nextTick, ref, watch } from 'vue'
const props = defineProps({ open: Boolean, message: { type: String, default: '' } })
const emit = defineEmits(['dismiss'])
const el = ref(null)
watch(() => props.open, async (v) => {
  await nextTick()
  const d = el.value
  if (!d) return
  if (v && !d.open) { d.showModal ? d.showModal() : d.setAttribute('open', '') }
  if (!v && d.open) { d.close ? d.close() : d.removeAttribute('open') }
}, { immediate: true })
</script>
<template>
  <dialog ref="el" class="confirm-dialog text-center" aria-labelledby="paused-title" @cancel.prevent="emit('dismiss')" data-testid="demo-paused">
    <span class="d-block mb-2" style="font-size: 2.25rem" aria-hidden="true">🌙</span>
    <h2 id="paused-title" class="h5">This demo is taking a short break</h2>
    <p class="mb-3">{{ message || 'This demo is temporarily paused. Check back soon.' }}</p>
    <button type="button" class="btn btn-primary w-100" autofocus @click="emit('dismiss')" data-testid="demo-paused-ok">Okay</button>
  </dialog>
</template>
