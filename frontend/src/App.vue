<script setup>
import { computed, onBeforeUnmount, onMounted } from 'vue'
import HonestyBanner from './components/HonestyBanner.vue'
import ModeIndicator from './components/ModeIndicator.vue'
import CityChip from './components/CityChip.vue'
import ServerStatus from './components/ServerStatus.vue'
import RateLimitNotice from './components/RateLimitNotice.vue'
import RateLimitCTA from './components/RateLimitCTA.vue'
import PausedNotice from './components/PausedNotice.vue'
import { useRouter } from 'vue-router'
import ConfirmDialog from './components/ConfirmDialog.vue'
import { boot, store, resetAll, selectCity, dismissQuotaCta, dismissDemoPaused } from './store.js'
import { clearedOnCitySwitch } from './lib/names.js'
import { hasUnsavedWork } from './lib/tripSummary.js'
import { PROJECT_NAME, REPO_URL } from './brand.js'
const router = useRouter()
function reset() { resetAll(); router.push('/') }
const pendingName = computed(() => store.cities.find((c) => c.id === store.pendingCity)?.display_name ?? '')
async function confirmSwitch() {
  const id = store.pendingCity
  store.pendingCity = null
  if (id) { await selectCity(id); router.push('/') }
}
function beforeUnload(e) {
  if (!hasUnsavedWork(store)) return
  e.preventDefault()
  e.returnValue = ''
}
onMounted(() => {
  boot()
  window.addEventListener('beforeunload', beforeUnload)
})
onBeforeUnmount(() => window.removeEventListener('beforeunload', beforeUnload))
</script>
<template>
  <a class="skip-link" href="#main">Skip to content</a>
  <header class="site-header">
    <div class="page d-flex flex-wrap align-items-center justify-content-between gap-2 py-2">
      <router-link to="/" class="wordmark"><span class="dot" aria-hidden="true"></span>{{ PROJECT_NAME }}</router-link>
      <nav aria-label="Steps">
        <ul class="nav-steps">
          <li><router-link to="/" exact-active-class="router-link-active">Plan</router-link></li>
          <li><router-link to="/trip">Your trip</router-link></li>
          <li><router-link to="/proof">Proof</router-link></li>
          <li><router-link to="/how-it-works">How it works</router-link></li>
        </ul>
      </nav>
    </div>
  </header>
  <HonestyBanner />
  <main id="main" class="page pt-3" tabindex="-1">
    <div class="stack mb-3">
      <div class="d-flex flex-wrap gap-2 align-items-center mode-slot"><CityChip /><ModeIndicator /></div>
      <ServerStatus />
      <div v-if="store.server === 'ready' && store.missingFeatures.length" class="notice notice-warn" role="alert" data-testid="server-outdated">
        <span class="glyph" aria-hidden="true">!</span>
        <span><strong>The server is older than this page.</strong> It is missing: {{ store.missingFeatures.join(', ') }}. Restart the backend from the latest code and reload. Until then some tools (such as the itinerary builder's live check) will not work, and a result that lacks the validator's verdict is shown as unchecked.</span>
      </div>
      <RateLimitNotice />
    </div>
    <router-view v-if="store.server === 'ready'" />
    <p v-else-if="store.server !== 'down'" class="muted" aria-live="polite">The planner will appear as soon as the server answers.</p>
  </main>
  <ConfirmDialog v-if="store.pendingCity" :open="true" :title="`Switch to ${pendingName}?`" confirm-label="Switch and clear" cancel-label="Keep current city" @confirm="confirmSwitch" @cancel="store.pendingCity = null">
    <p class="mb-2">One trip covers one city, so switching to {{ pendingName }} will clear:</p>
    <ul class="mb-0" data-testid="switch-clears"><li v-for="item in clearedOnCitySwitch(store)" :key="item">{{ item }}</li></ul>
  </ConfirmDialog>
  <RateLimitCTA v-if="store.quotaCta.open" :queries-made="store.quotaCta.count" @dismiss="dismissQuotaCta" />
  <PausedNotice v-if="store.demoPaused.open" :open="true" :message="store.demoPaused.message" @dismiss="dismissDemoPaused" />
  <footer class="page site-footer" data-testid="footer">
    <p class="mb-1"><strong>Privacy:</strong> nothing is stored; no cookies or analytics. Map tiles come from OpenStreetMap. <router-link to="/how-it-works#privacy">Details</router-link></p>
    <p class="mb-0">{{ PROJECT_NAME }} · <router-link to="/proof">Proof</router-link> · <a :href="REPO_URL" target="_blank" rel="noopener noreferrer">Source</a> ·
      <button type="button" class="btn btn-link btn-sm p-0 align-baseline" @click="reset" data-testid="reset" title="Clear everything held in this page">Reset</button></p>
  </footer>
</template>
