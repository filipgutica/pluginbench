import { UiCodeBlock } from '@filipgutica/ui';
import type { Element, ElementContent, Properties } from 'hast';
import { fromHtml } from 'hast-util-from-html';
import { toHtml } from 'hast-util-to-html';
import { SKIP, visit } from 'unist-util-visit';
import { createSSRApp, h } from 'vue';
import { renderToString } from 'vue/server-renderer';
import { continuesCommand, libraryCodeLanguages } from './code-blocks';
import { documents } from './pages';

const repository = 'https://github.com/filipgutica/pluginbench/blob/main/';
// Tables this wide need more than a phone's width; narrower tables wrap their cells.
const wideTableColumns = 4;

const element = (tagName: string, properties: Properties, children: ElementContent[]): Element => ({
  type: 'element',
  tagName,
  properties,
  children,
});

const textOf = (node: ElementContent): string => {
  if (node.type === 'text') return node.value;
  return node.type === 'element' ? node.children.map(textOf).join('') : '';
};

const columnCount = (table: Element) => {
  let count = 0;
  visit(table, 'element', (node) => {
    if (node.tagName !== 'tr') return;
    count = node.children.filter((cell) => cell.type === 'element').length;
    return false;
  });
  return count;
};

const hasClass = (node: Element, name: string) => Array.isArray(node.properties.className) && node.properties.className.includes(name);

/**
 * Renders a fence with the library's UiCodeBlock, without Copy until scripts mount it.
 * An untitled `sh` block is a command plate whose lines get a `$` prompt; lines that
 * continue a command after a trailing backslash are marked so they get none.
 */
const renderCodeBlock = async ({ code, language }: { code: string; language: string }) => {
  const html = await renderToString(createSSRApp({ render: () => h(UiCodeBlock, { code, language, copyable: false }) }));
  const block = fromHtml(html, { fragment: true });
  const continued = continuesCommand(code);
  let line = 0;
  visit(block, 'element', (node) => {
    if (!hasClass(node, 'fg-code-block__line')) return;
    if (continued[line]) node.properties.dataContinuation = '';
    line += 1;
  });
  return block.children.filter((node): node is ElementContent => node.type !== 'doctype');
};

/**
 * Adapts Astro's rendered Markdown to the site frame. It works on the parsed
 * tree, so text in inline code and code samples is never rewritten.
 * - Repository-relative links to published documents point at their site pages;
 *   other relative links point at the file on GitHub.
 * - `sh`, `yaml`, and `json` samples render as the library's UiCodeBlock; other
 *   samples lose Shiki's inline theme, so the library's code block styles them.
 * - Tables scroll inside a keyboard-focusable region named after their section;
 *   wide ones get a scroll cue.
 * - Each h2 starts a section, which opts it into the library's ruled ## heading.
 */
export const transformDocumentHtml = async ({ html, source, pageHref }: {
  html: string;
  /** Repository path of the Markdown file, which relative links resolve against. */
  source: string;
  pageHref: (slug: string) => string;
}): Promise<string> => {
  const tree = fromHtml(html, { fragment: true });
  const codeBlocks: { container: Element; code: string; language: string }[] = [];

  visit(tree, 'element', (node, index, parent) => {
    if (node.tagName === 'a' && typeof node.properties.href === 'string') {
      const { href } = node.properties;
      if (/^(?:[a-z][\w+.-]*:|#|\/\/)/i.test(href)) return;
      const target = new URL(href, `${repository}${source}`);
      const published = documents.find((page) => target.pathname === new URL(page.source, repository).pathname);
      node.properties.href = published ? `${pageHref(published.slug)}${target.search}${target.hash}` : target.href;
      return;
    }
    if (!parent || index === undefined) return;
    if (node.tagName === 'pre') {
      const language = String(node.properties.dataLanguage ?? '');
      if (libraryCodeLanguages.includes(language)) {
        // The client script mounts UiCodeBlock into this container to add Copy.
        const container = element('div', { dataCodeLanguage: language }, []);
        codeBlocks.push({ container, code: textOf(node), language });
        parent.children[index] = container;
        return SKIP;
      }
      const code = element('code', {}, [{ type: 'text', value: textOf(node) }]);
      parent.children[index] = element('div', { className: ['fg-code-block'] }, [
        element('pre', { className: ['fg-code-block__pre'], tabIndex: 0 }, [code]),
      ]);
      return SKIP;
    }
    if (node.tagName === 'table') {
      const region = element('div', { className: ['table-scroll'], role: 'region', tabIndex: 0 }, [node]);
      const replacement = columnCount(node) >= wideTableColumns
        ? [element('p', { className: ['fg-site-meta', 'table-cue'] }, [{ type: 'text', value: 'Scroll the table sideways to see every column.' }]), region]
        : [region];
      parent.children.splice(index, 1, ...replacement);
      // Carry on into the table's cells, whose links still need rewriting, then past the replacement.
      return index + replacement.length;
    }
  });

  for (const { container, code, language } of codeBlocks) {
    container.children = await renderCodeBlock({ code, language });
  }

  const sections: Element[] = [];
  // Table regions take the name of their section's h2, or of the h1 before the first h2.
  // Several tables under one heading are numbered, so each region's name is unique.
  const regionsByHeading: { heading?: Element; regions: Element[] }[] = [{ regions: [] }];
  for (const node of tree.children) {
    if (node.type === 'doctype') continue;
    const startsSection = node.type === 'element' && node.tagName === 'h2';
    if (startsSection || !sections.length) sections.push(element('section', {}, []));
    sections[sections.length - 1].children.push(node);
    if (node.type !== 'element') continue;
    if (['h1', 'h2'].includes(node.tagName) && typeof node.properties.id === 'string') regionsByHeading.push({ heading: node, regions: [] });
    visit(node, 'element', (child) => {
      if (hasClass(child, 'table-scroll')) regionsByHeading[regionsByHeading.length - 1].regions.push(child);
    });
  }
  for (const { heading, regions } of regionsByHeading) {
    regions.forEach((region, index) => {
      if (!heading) region.properties.ariaLabel = 'Table';
      else if (regions.length === 1) region.properties.ariaLabelledBy = [String(heading.properties.id)];
      else region.properties.ariaLabel = `${textOf(heading)}, table ${index + 1} of ${regions.length}`;
    });
  }
  return toHtml(sections);
};
