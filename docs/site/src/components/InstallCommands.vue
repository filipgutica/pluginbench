<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { UiCodeBlock } from '@filipgutica/ui';

// Copy needs the clipboard, so it appears only after hydration.
const enhanced = ref(false);
onMounted(() => { enhanced.value = true; });

// One command, so the compact variant puts Copy beside it.
const code = 'pipx install pluginbench';
</script>

<template>
  <UiCodeBlock :code="code" language="bash" variant="compact" :copyable="enhanced" />
</template>
