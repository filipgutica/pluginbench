<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue';
import { SITE_PROJECTS, UiSiteAppearancePicker, useSiteAppearance } from '@filipgutica/ui/site';
import type { UiSiteLink } from '@filipgutica/ui/site';

// Replace this component with UiSiteHeader once its registry includes PluginBench.
defineProps<{ project: 'pluginbench'; links: readonly UiSiteLink[] }>();
const projects = [...SITE_PROJECTS, { id: 'pluginbench', label: 'PluginBench', href: 'https://filipgutica.github.io/pluginbench/' }];
const appearance = useSiteAppearance();
const mounted = ref(false);
const menu = ref<HTMLDetailsElement>();
const trigger = ref<HTMLElement>();
const closeMenu = () => { if (menu.value) menu.value.open = false; };
const onKeydown = (event: KeyboardEvent) => {
  if (event.key !== 'Escape' || !menu.value?.open) return;
  closeMenu();
  trigger.value?.focus();
};
const onFocusout = (event: FocusEvent) => {
  if (event.relatedTarget instanceof Node && !menu.value?.contains(event.relatedTarget)) closeMenu();
};
const onDocumentPointerdown = (event: PointerEvent) => {
  if (event.target instanceof Node && !menu.value?.contains(event.target)) closeMenu();
};
onMounted(() => {
  mounted.value = true;
  document.addEventListener('pointerdown', onDocumentPointerdown);
});
onBeforeUnmount(() => document.removeEventListener('pointerdown', onDocumentPointerdown));
</script>

<template>
  <header class="fg-site-header">
    <div class="fg-site-header__inner">
      <details ref="menu" class="fg-site-projects" @keydown="onKeydown" @focusout="onFocusout">
        <summary ref="trigger" class="fg-site-projects__trigger fg-quiet-action" aria-label="Project: PluginBench">
          <span>PluginBench</span>
          <svg class="fg-site-projects__chevron" viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
            <path d="m4 6 4 4 4-4" />
          </svg>
        </summary>
        <nav class="fg-site-projects__menu fg-site-nav" aria-label="Projects">
          <a v-for="item in projects" :key="item.id" :href="item.href" :aria-current="item.id === project ? 'page' : undefined">{{ item.label }}</a>
        </nav>
      </details>
      <div class="fg-site-header__end">
        <nav class="fg-site-header__links" aria-label="Site links">
          <a v-for="link in links" :key="link.href" :href="link.href">{{ link.label }}</a>
        </nav>
        <UiSiteAppearancePicker v-if="mounted" :model-value="appearance.choice.value" @update:model-value="appearance.chooseAppearance" />
      </div>
    </div>
  </header>
</template>
