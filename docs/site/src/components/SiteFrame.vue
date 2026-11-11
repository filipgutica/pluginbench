<script setup lang="ts">
import { UiSiteNav } from '@filipgutica/ui/site';
import type { UiSiteNavGroup } from '@filipgutica/ui/site';
import SiteHeader from './SiteHeader.vue';

defineProps<{ groups: UiSiteNavGroup[] }>();
const headerLinks = [
  { label: 'GitHub', href: 'https://github.com/filipgutica/pluginbench' },
  { label: 'Releases', href: 'https://github.com/filipgutica/pluginbench/releases' },
];
</script>

<template>
  <a class="fg-site-skip" href="#main">Skip to content</a>
  <div class="fg-site-docs">
    <SiteHeader project="pluginbench" :links="headerLinks" />
    <UiSiteNav label="On this page" :groups="groups" />
    <main id="main" class="fg-site-prose doc-content">
      <slot />
    </main>
    <footer class="fg-site-footer">
      <a href="https://github.com/filipgutica">Filip Gutica</a>
      <nav aria-label="Project links">
        <a href="https://github.com/filipgutica/pluginbench/issues">Issues</a>
        <a href="https://github.com/filipgutica/pluginbench/releases">Releases</a>
      </nav>
    </footer>
  </div>
</template>
