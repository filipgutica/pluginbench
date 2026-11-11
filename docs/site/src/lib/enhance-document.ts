import { createApp, h } from 'vue';
import { UiCodeBlock } from '@filipgutica/ui';
import { continuesCommand } from './code-blocks';

const markContinuations = ({ container, code }: { container: Element; code: string }) => {
  const continued = continuesCommand(code);
  container.querySelectorAll('.fg-code-block__line').forEach((line, index) => line.toggleAttribute('data-continuation', continued[index]));
};

// Markdown code blocks are built from UiCodeBlock without Copy, so there is no dead
// button without scripts. Mounting the component here adds Copy and client highlighting.
const mountCodeBlocks = () => {
  for (const container of document.querySelectorAll<HTMLElement>('[data-code-language]')) {
    const code = container.querySelector('code')?.textContent ?? '';
    createApp({ render: () => h(UiCodeBlock, { code, language: container.dataset.codeLanguage }) }).mount(container);
    markContinuations({ container, code });
  }
};

// Code and tables render as Tab stops so they scroll by keyboard without scripts;
// once scripts run, only the ones that overflow stay in the Tab order.
const updateTabStop = (element: HTMLElement) => {
  const scrolls = element.scrollWidth > element.clientWidth || element.scrollHeight > element.clientHeight;
  if (scrolls) element.tabIndex = 0;
  else element.removeAttribute('tabindex');
};

const trackScrollContainers = () => {
  const elements = [...document.querySelectorAll<HTMLElement>('main :is(.fg-code-block__pre, .table-scroll)')];
  const observer = new ResizeObserver((entries) => entries.forEach(({ target }) => updateTabStop(target as HTMLElement)));
  elements.forEach((element) => observer.observe(element));
  // Web fonts change how wide code is without resizing its box.
  void document.fonts.ready.then(() => elements.forEach(updateTabStop));
};

const enhance = () => {
  mountCodeBlocks();
  trackScrollContainers();
};

// The frame island resets its slot's HTML when it hydrates, so enhance the content after that.
const frame = document.querySelector('main')?.closest('astro-island[ssr]');
if (frame) frame.addEventListener('astro:hydrate', enhance, { once: true });
else enhance();
