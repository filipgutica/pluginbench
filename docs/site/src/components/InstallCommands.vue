<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { UiCodeBlock } from '@filipgutica/ui';

// Copy needs the clipboard, so it appears only after hydration.
const enhanced = ref(false);
onMounted(() => { enhanced.value = true; });

// Untitled, so the library draws a command plate with $ prompts. Long lines scroll rather than wrap.
const code = [
  'git clone https://github.com/filipgutica/pluginbench.git',
  'cd pluginbench',
  'python3 -m venv .venv',
  'source .venv/bin/activate',
  'pip install -e .',
].join('\n');
</script>

<template>
  <UiCodeBlock :code="code" language="bash" :copyable="enhanced" />
</template>
