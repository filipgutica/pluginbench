import { expect, test } from '@playwright/test';
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
  ].join('');
  const output = transformDocumentHtml({ html, source: 'docs/running-evaluations.md', pageHref });
  await page.setContent(output);

  await expect(page.locator('p code')).toHaveText('href="config-reference.md"');
  expect(await page.locator('pre code').evaluate((element) => element.textContent)).toBe(`<a href="metrics.md">\n<a href='reproduce.md'>`);
  await expect(page.getByRole('link', { name: 'gates' })).toHaveAttribute('href', '/pluginbench/config-reference/#gates-file');
  await expect(page.getByRole('link', { name: 'metrics' })).toHaveAttribute('href', '/pluginbench/metrics/');
  await expect(page.getByRole('link', { name: 'install' })).toHaveAttribute('href', 'https://github.com/filipgutica/pluginbench/blob/main/README.md#install');

  // Code samples take the library's code block styles: no Shiki theme and one tabindex.
  const pre = page.locator('.fg-code-block > pre.fg-code-block__pre');
  await expect(pre).toHaveAttribute('class', 'fg-code-block__pre');
  await expect(pre).not.toHaveAttribute('style', /./);
  await expect(pre.locator('span')).toHaveCount(0);
  expect(output.match(/tabindex/gi)).toHaveLength(1);
});
