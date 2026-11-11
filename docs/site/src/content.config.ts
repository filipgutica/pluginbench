import { defineCollection } from 'astro:content';
import { z } from 'astro/zod';
import type { Loader } from 'astro/loaders';
import { readFile } from 'node:fs/promises';
import { relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { pageHref, pages } from './lib/pages';

const repository = 'https://github.com/filipgutica/pluginbench/blob/main/';

const docsLoader: Loader = {
  name: 'pluginbench-docs',
  async load({ config, renderMarkdown, store, watcher }) {
    const root = new URL('../../', config.root);
    const loadPage = async (page: (typeof pages)[number]) => {
      const fileURL = new URL(page.source, root);
      const body = await readFile(fileURL, 'utf8');
      const rendered = await renderMarkdown(body, { fileURL });

      // These transformations consume Astro's generated HTML, never Markdown text
      // or code examples. Heading IDs and GFM tables remain owned by Astro.
      rendered.html = rendered.html.replace(/href="([^"]*)"/g, (attribute, href: string) => {
        if (/^(?:[a-z][\w+.-]*:|#|\/\/)/i.test(href)) return attribute;
        const target = new URL(href, `${repository}${page.source}`);
        const published = pages.find(({ source }) => target.pathname === new URL(source, repository).pathname);
        return `href="${published ? `${pageHref(published.slug)}${target.search}${target.hash}` : target.href}"`;
      });
      rendered.html = rendered.html
        .replace(/<table>/g, '<div class="table-scroll" role="region" aria-label="Scrollable table" tabindex="0"><table>')
        .replace(/<\/table>/g, '</table></div>')
        .replace(/<pre class="/g, '<div class="fg-code-block"><pre tabindex="0" class="fg-code-block__pre ')
        .replace(/<\/pre>/g, '</pre></div>');
      // Section wrappers opt Markdown h2s into the library's ruled ## headings.
      rendered.html = `<section>${rendered.html.replace(/(?=<h2\s)/g, '</section><section>')}</section>`;
      store.set({ id: page.slug || 'home', data: page, body, rendered, filePath: relative(fileURLToPath(config.root), fileURLToPath(fileURL)) });
    };

    store.clear();
    for (const page of pages) await loadPage(page);
    watcher?.add(pages.map(({ source }) => fileURLToPath(new URL(source, root))));
    watcher?.on('change', async (path) => {
      const page = pages.find(({ source }) => fileURLToPath(new URL(source, root)) === path);
      if (page) await loadPage(page);
    });
  },
};

export const collections = {
  docs: defineCollection({
    loader: docsLoader,
    schema: z.object({ slug: z.string(), title: z.string(), description: z.string() }),
  }),
};
