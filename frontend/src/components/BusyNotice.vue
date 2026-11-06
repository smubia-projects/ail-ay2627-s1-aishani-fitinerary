<script setup>
// Friendly message for 423 solver_busy: the server solves one plan at a time. A countdown, then a MANUAL Retry button. Nothing is retried automatically.
import { computed } from 'vue'
import { solverBusyActive, solverBusySeconds, store } from '../store.js'
const props = defineProps({ error: { type: Object, default: null } })
defineEmits(['retry'])
const show = computed(() => props.error?.code === 'solver_busy')
</script>
<template>
  <div v-if="show" class="notice notice-info" role="status" aria-live="polite" data-testid="solver-busy">
    <span class="glyph" aria-hidden="true">i</span>
    <div>
      <strong>Another plan is still being calculated.</strong> Try again in a few seconds.
      <div class="small mt-1" data-testid="busy-countdown"><template v-if="solverBusyActive">You can retry in {{ solverBusySeconds }} s.</template><template v-else>You can retry now.</template></div>
      <button type="button" class="btn btn-sm btn-outline-primary mt-2" :disabled="solverBusyActive || store.busy.optimise || store.busy.compare" @click="$emit('retry')" data-testid="busy-retry">Retry</button>
    </div>
  </div>
</template>
