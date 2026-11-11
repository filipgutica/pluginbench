import { defineCollection } from 'astro:content';
import { z } from 'astro/zod';
import type { Loader } from 'astro/loaders';
import { readFile } from 'node:fs/promises';
import { relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { transformDocumentHtml } from './lib/document-html';
import { documents, pageHref } from './lib/pages';

const docsLoader: Loader = {
  name: 'pluginbench-docs',
  async load({ config, renderMarkdown, store, watcher }) {
    const root = new URL('../../', config.root);
    const loadPage = async (page: (typeof documents)[number]) => {
      const fileURL = new URL(page.source, root);
      const body = await readFile(fileURL, 'utf8');
      const rendered = await renderMarkdown(body, { fileURL });
      // Heading IDs and GFM tables remain owned by Astro.
      rendered.html = await transformDocumentHtml({ html: rendered.html, source: page.source, pageHref });
      store.set({ id: page.slug, data: page, body, rendered, filePath: relative(fileURLToPath(config.root), fileURLToPath(fileURL)) });
    };

    store.clear();
    for (const page of documents) await loadPage(page);
    watcher?.add(documents.map(({ source }) => fileURLToPath(new URL(source, root))));
    watcher?.on('change', async (path) => {
      const page = documents.find(({ source }) => fileURLToPath(new URL(source, root)) === path);
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
