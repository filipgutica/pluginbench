import type { Element, ElementContent, Properties } from 'hast';
import { fromHtml } from 'hast-util-from-html';
import { toHtml } from 'hast-util-to-html';
import { SKIP, visit } from 'unist-util-visit';
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

/**
 * Adapts Astro's rendered Markdown to the site frame. It works on the parsed
 * tree, so text in inline code and code samples is never rewritten.
 * - Repository-relative links to published documents point at their site pages;
 *   other relative links point at the file on GitHub.
 * - Code samples lose Shiki's inline theme, so the library's code block styles them.
 * - Tables scroll inside a keyboard-focusable region; wide ones get a scroll cue.
 * - Each h2 starts a section, which opts it into the library's ruled ## heading.
 */
export const transformDocumentHtml = ({ html, source, pageHref }: {
  html: string;
  /** Repository path of the Markdown file, which relative links resolve against. */
  source: string;
  pageHref: (slug: string) => string;
}): string => {
  const tree = fromHtml(html, { fragment: true });

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
      const code = element('code', {}, [{ type: 'text', value: textOf(node) }]);
      parent.children[index] = element('div', { className: ['fg-code-block'] }, [
        element('pre', { className: ['fg-code-block__pre'], tabIndex: 0 }, [code]),
      ]);
      return SKIP;
    }
    if (node.tagName === 'table') {
      const region = element('div', { className: ['table-scroll'], role: 'region', ariaLabel: 'Scrollable table', tabIndex: 0 }, [node]);
      const replacement = columnCount(node) >= wideTableColumns
        ? [element('p', { className: ['fg-site-meta', 'table-cue'] }, [{ type: 'text', value: 'Scroll the table sideways to see every column.' }]), region]
        : [region];
      parent.children.splice(index, 1, ...replacement);
      return [SKIP, index + replacement.length];
    }
  });

  const sections: Element[] = [];
  for (const node of tree.children) {
    if (node.type === 'doctype') continue;
    const startsSection = node.type === 'element' && node.tagName === 'h2';
    if (startsSection || !sections.length) sections.push(element('section', {}, []));
    sections[sections.length - 1].children.push(node);
  }
  return toHtml(sections);
};
