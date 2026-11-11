import { expect, test } from '@playwright/test';
import { continuesCommand } from '../src/lib/code-blocks';
import { transformDocumentHtml } from '../src/lib/document-html';

const pageHref = (slug: string) => `/pluginbench/${slug ? `${slug}/` : ''}`;

test('links are rewritten only in anchors, never in code, for either quote style', async ({ page }) => {
  // Markup in the shape Astro renders: inline code, a Shiki-highlighted fence, and real links.
  const html = [
    '<h1 id="sample">Sample</h1>',
    '<p>Write <code>href="config-reference.md"</code> to link it.</p>',
    '<pre class="astro-code github-dark" style="background-color:#24292e" tabindex="0" data-language="html"><code>',
    '<span class="line"><span style="color:#85E89D">&lt;a href="metrics.md"&gt;</span></span>\n',
    "<span class=\"line\"><span style=\"color:#E1E4E8\">&lt;a href='reproduce.md'&gt;</span></span>",
    '</code></pre>',
    "<p><a href='config-reference.md#gates-file'>gates</a> and <a href=\"metrics.md\">metrics</a> and <a href=\"../README.md#install\">install</a></p>",
    '<table><thead><tr><th>Gate</th></tr></thead><tbody><tr><td><a href="metrics.md#correctness">table metric</a></td></tr></tbody></table>',
  ].join('');
  const output = await transformDocumentHtml({ html, source: 'docs/running-evaluations.md', pageHref });
  await page.setContent(output);

  await expect(page.locator('p code')).toHaveText('href="config-reference.md"');
  expect(await page.locator('pre code').evaluate((element) => element.textContent)).toBe(`<a href="metrics.md">\n<a href='reproduce.md'>`);
  await expect(page.getByRole('link', { name: 'gates' })).toHaveAttribute('href', '/pluginbench/config-reference/#gates-file');
  await expect(page.getByRole('link', { name: 'metrics' })).toHaveAttribute('href', '/pluginbench/metrics/');
  await expect(page.getByRole('link', { name: 'install' })).toHaveAttribute('href', 'https://github.com/filipgutica/pluginbench/blob/main/README.md#install');
  await expect(page.locator('.table-scroll a')).toHaveAttribute('href', '/pluginbench/metrics/#correctness');

  // Code samples take the library's code block styles: no Shiki theme and one tabindex.
  const pre = page.locator('.fg-code-block > pre.fg-code-block__pre');
  await expect(pre).toHaveAttribute('class', 'fg-code-block__pre');
  await expect(pre).not.toHaveAttribute('style', /./);
  await expect(pre.locator('span')).toHaveCount(0);
  const codeBlock = output.match(/<div class="fg-code-block">.*?<\/div>/s)?.[0] ?? '';
  expect(codeBlock.match(/tabindex/gi)).toHaveLength(1);
});

test('sh fences render as library command blocks; continuation lines are marked for no prompt', async ({ page }) => {
  const html = [
    '<h1 id="sample">Sample</h1>',
    '<pre class="astro-code github-dark" style="background-color:#24292e" tabindex="0" data-language="sh"><code>',
    '<span class="line"><span>pluginbench judge prepare a=x b=y \\</span></span>\n',
    '<span class="line"><span>  --tasks task-a \\</span></span>\n',
    '<span class="line"><span>  --seed 11</span></span>\n',
    '<span class="line"><span>pluginbench judge tally out</span></span>',
    '</code></pre>',
  ].join('');
  const output = await transformDocumentHtml({ html, source: 'docs/running-evaluations.md', pageHref });
  await page.setContent(output);

  const block = page.locator('[data-code-language="sh"] > .fg-code-block');
  await expect(block).toHaveAttribute('data-command', 'true');
  expect(await block.locator('code').evaluate((element) => element.textContent)).toBe('pluginbench judge prepare a=x b=y \\\n  --tasks task-a \\\n  --seed 11\npluginbench judge tally out');
  const lines = block.locator('.fg-code-block__line');
  expect(await lines.evaluateAll((elements) => elements.map((element) => element.hasAttribute('data-continuation')))).toEqual([false, true, true, false]);
  // No Copy until scripts mount the component, and the one tabindex is the library's.
  await expect(block.getByRole('button')).toHaveCount(0);
  expect(output.match(/tabindex/gi)).toHaveLength(1);
});

test('only an unescaped backslash outside comments, or an open quote, continues a shell command', () => {
  const lines = [
    'pluginbench run a \\',
    "  --flag 'x\\'",
    "printf '%s\\n' \\\\",
    'echo NEXT # ends in \\',
    'echo "open',
    'quote"',
    'echo done',
  ];
  expect(continuesCommand(lines.join('\n'))).toEqual([false, true, false, false, false, true, false]);
});

test('table regions are named after their section heading, or the page title before the first section', async ({ page }) => {
  const table = '<table><thead><tr><th>Key</th></tr></thead><tbody><tr><td>value</td></tr></tbody></table>';
  const html = `<h1 id="page">Page</h1>${table}<h2 id="first">First</h2><p>Text</p>${table}`;
  await page.setContent(await transformDocumentHtml({ html, source: 'docs/metrics.md', pageHref }));
  await expect(page.getByRole('region')).toHaveCount(2);
  await expect(page.getByRole('region', { name: 'Page', exact: true })).toHaveCount(1);
  await expect(page.getByRole('region', { name: 'First', exact: true })).toHaveCount(1);
});

test('several tables under one heading get numbered region names', async ({ page }) => {
  const table = '<table><thead><tr><th>Key</th></tr></thead><tbody><tr><td>value</td></tr></tbody></table>';
  const html = `<h1 id="page">Page</h1><h2 id="first">First</h2>${table}<p>Text</p>${table}`;
  await page.setContent(await transformDocumentHtml({ html, source: 'docs/metrics.md', pageHref }));
  await expect(page.getByRole('region', { name: 'First, table 1 of 2', exact: true })).toHaveCount(1);
  await expect(page.getByRole('region', { name: 'First, table 2 of 2', exact: true })).toHaveCount(1);
});
