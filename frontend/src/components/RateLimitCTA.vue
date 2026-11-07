<script setup>
// AI Lodge Rate-Limit CTA modal. Shown when the shared rate-limit engine answers 429 (code ai_quota_reached): the count comes
// from the backend (detail.queries_used), never from a client-side counter. Dismissing only hides it; the limit stays.
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'

const PROJECT_CONFIG = {
  name: 'Fitinerary',
  emoji: '🧳',
  accentColor: '#ad472f',
  headline: 'Your trip is taking shape!',
  description: "Want to build AI that untangles hotel prices, budgets and day-by-day schedules for travellers? AI Lodge puts you in a small student team to build a real AI product and demo it at a live showcase.",
  programme: 'AI Lodge',
  programmeLink: 'https://www.smubia.com/ailodge',
  githubLink: 'https://github.com/smubia-projects/ail-ay2627-s1-aishani-fitinerary',
  showcaseLink: 'https://www.smubia.com/Projects',
}

const props = defineProps({ queriesMade: { type: Number, default: null } })
const emit = defineEmits(['dismiss'])
const card = ref(null)
let opener = null

function hexToHsl(hex) {
  const r = parseInt(hex.slice(1, 3), 16) / 255
  const g = parseInt(hex.slice(3, 5), 16) / 255
  const b = parseInt(hex.slice(5, 7), 16) / 255
  const max = Math.max(r, g, b), min = Math.min(r, g, b)
  let h = 0, s = 0
  const l = (max + min) / 2
  if (max !== min) {
    const d = max - min
    s = l > 0.5 ? d / (2 - max - min) : d / (max + min)
    if (max === r) h = ((g - b) / d + (g < b ? 6 : 0)) / 6
    else if (max === g) h = ((b - r) / d + 2) / 6
    else h = ((r - g) / d + 4) / 6
  }
  return [Math.round(h * 360), Math.round(s * 100), Math.round(l * 100)]
}
const [h, s, l] = hexToHsl(PROJECT_CONFIG.accentColor)
const bannerStyle = { background: `linear-gradient(135deg, hsl(${h}, ${s}%, ${l + 15}%), hsl(${h}, ${s}%, ${l}%), hsl(${h}, ${s}%, ${l - 10}%))`, backgroundSize: '200% 200%' }
const primaryStyle = { background: `linear-gradient(135deg, hsl(${h}, ${s}%, ${l + 5}%), hsl(${h}, ${s}%, ${l - 5}%))`, boxShadow: `0 4px 14px hsla(${h}, ${s}%, ${l}%, 0.4)` }
const description = computed(() => PROJECT_CONFIG.description.replace('{count}', props.queriesMade ?? 'the'))

function focusables() { return card.value ? [...card.value.querySelectorAll('a[href], button')] : [] }
function onKey(e) {
  if (e.key === 'Escape') { e.preventDefault(); emit('dismiss'); return }
  if (e.key !== 'Tab') return
  const f = focusables()
  if (!f.length) return
  const first = f[0], last = f[f.length - 1]
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus() }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus() }
}
onMounted(async () => {
  opener = document.activeElement
  document.addEventListener('keydown', onKey)
  await nextTick()
  focusables()[0]?.focus()
})
onBeforeUnmount(() => {
  document.removeEventListener('keydown', onKey)
  opener?.focus?.()
})
</script>

<template>
  <div class="rl-backdrop" @click.self="emit('dismiss')" data-testid="rate-limit-cta">
    <div ref="card" class="rl-card" role="dialog" aria-modal="true" aria-labelledby="rl-title" aria-describedby="rl-desc">
      <div class="rl-banner" :style="bannerStyle">
        <span class="rl-circle rl-circle-a" aria-hidden="true"></span>
        <span class="rl-circle rl-circle-b" aria-hidden="true"></span>
        <div class="rl-banner-inner">
          <span class="rl-emoji" aria-hidden="true">{{ PROJECT_CONFIG.emoji }}</span>
          <h2 id="rl-title" class="rl-headline">{{ PROJECT_CONFIG.headline }}</h2>
          <p id="rl-desc" class="rl-desc">{{ description }}</p>
        </div>
      </div>
      <div class="rl-body">
        <a :href="PROJECT_CONFIG.programmeLink" target="_blank" rel="noopener noreferrer" class="rl-btn rl-btn-primary" :style="primaryStyle">
          Join {{ PROJECT_CONFIG.programme }}<span class="rl-arrow" aria-hidden="true">→</span>
        </a>
        <a :href="PROJECT_CONFIG.showcaseLink" target="_blank" rel="noopener noreferrer" class="rl-btn rl-btn-secondary">
          Explore other projects<span class="rl-arrow" aria-hidden="true">→</span>
        </a>
        <a :href="PROJECT_CONFIG.githubLink" target="_blank" rel="noopener noreferrer" class="rl-btn rl-btn-secondary">
          View on GitHub<span class="rl-arrow" aria-hidden="true">→</span>
        </a>
        <button type="button" class="rl-later" @click="emit('dismiss')" data-testid="rate-limit-cta-dismiss">Maybe later</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.rl-backdrop {
  position: fixed; inset: 0; z-index: 2000; display: flex; align-items: center; justify-content: center; padding: 16px;
  animation: rl-backdrop-in 500ms ease forwards;
}
.rl-card {
  width: 100%; max-width: 440px; border-radius: 16px; overflow: hidden; box-shadow: 0 24px 60px rgba(0, 0, 0, 0.35);
  animation: rl-card-in 600ms cubic-bezier(0.16, 1, 0.3, 1) forwards;
}
.rl-banner { position: relative; overflow: hidden; padding: 32px 24px; text-align: center; animation: rl-shimmer 6s ease infinite; }
.rl-circle { position: absolute; border-radius: 50%; }
.rl-circle-a { top: -32px; right: -32px; width: 128px; height: 128px; background: rgba(255, 255, 255, 0.12); }
.rl-circle-b { top: 64px; right: -16px; width: 80px; height: 80px; background: rgba(255, 255, 255, 0.08); }
.rl-banner-inner { position: relative; z-index: 1; }
.rl-emoji { display: block; font-size: 2.5rem; margin-bottom: 12px; }
.rl-headline { color: #fff; font-size: 1.5rem; margin-bottom: 8px; text-shadow: 0 1px 3px rgba(0, 0, 0, 0.3); }
.rl-desc { color: rgba(255, 255, 255, 0.88); font-size: 0.9rem; line-height: 1.55; margin: 0 auto; max-width: 340px; }
.rl-body { background: #18181b; padding: 24px; display: flex; flex-direction: column; gap: 12px; }
.rl-btn {
  position: relative; display: flex; align-items: center; justify-content: center; width: 100%; padding: 12px 44px;
  border-radius: 10px; color: #fff; font-weight: 600; font-size: 0.9rem; text-decoration: none; transition: transform 0.15s, border-color 0.15s;
}
.rl-btn:hover { color: #fff; transform: scale(1.02); }
.rl-btn-secondary { background: rgba(255, 255, 255, 0.06); border: 1px solid rgba(255, 255, 255, 0.12); color: rgba(255, 255, 255, 0.9); font-weight: 500; }
.rl-btn-secondary:hover { border-color: rgba(255, 255, 255, 0.3); }
.rl-arrow { position: absolute; right: 18px; opacity: 0.5; }
.rl-later { background: none; border: 0; padding: 8px; color: rgba(255, 255, 255, 0.45); font-size: 0.85rem; }
.rl-later:hover { color: rgba(255, 255, 255, 0.7); }
.rl-btn:focus-visible, .rl-later:focus-visible { outline: 3px solid #fff; outline-offset: 2px; }
@keyframes rl-backdrop-in {
  from { backdrop-filter: blur(0); background: rgba(0, 0, 0, 0); }
  to { backdrop-filter: blur(12px); background: rgba(0, 0, 0, 0.5); }
}
@keyframes rl-card-in {
  from { opacity: 0; transform: scale(0.95) translateY(10px); }
  to { opacity: 1; transform: scale(1) translateY(0); }
}
@keyframes rl-shimmer {
  0% { background-position: 0% 50%; }
  50% { background-position: 100% 50%; }
  100% { background-position: 0% 50%; }
}
@media (prefers-reduced-motion: reduce) {
  .rl-backdrop, .rl-card, .rl-banner { animation-duration: 1ms; animation-iteration-count: 1; }
}
</style>
