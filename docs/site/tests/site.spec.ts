import { expect, test as base } from '@playwright/test';
import { documents } from '../src/lib/pages';
import { expectNoOverflow, routes } from './pages';

const test = base.extend<{ cleanPage: void }>({
  cleanPage: [async ({ page }, use) => {
    const errors: string[] = [];
    page.on('pageerror', (error) => errors.push(error.message));
    page.on('console', (message) => {
      if (message.type() === 'error' || (/hydration/i.test(message.text()) && message.type() === 'warning')) errors.push(message.text());
    });
    page.on('response', (response) => {
      if (response.url().startsWith('http://127.0.0.1:4188/') && response.status() >= 400) errors.push(`${response.status()} ${response.url()}`);
    });
    await use();
    expect(errors).toEqual([]);
  }, { auto: true }],
});

for (const width of [320, 390, 720, 801, 1280]) {
  test(`all pages and navigation work at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto('/pluginbench/');
    for (const route of routes) {
      await expect(page.locator('.fg-site-rail')).toHaveAttribute('data-enhanced', 'true');
      if (width <= 800) await page.locator('.fg-site-rail__toggle').click();
      const nav = page.getByRole('navigation', { name: 'Documentation', exact: true });
      await expect(nav.locator('.fg-site-rail__group').first().getByRole('link')).toHaveText(routes.map(({ label }) => label));
      await expect(nav.getByRole('group', { name: 'On this page', exact: true })).toHaveCount(await page.locator('main h2').count() ? 1 : 0);
      await nav.getByRole('link', { name: route.label, exact: true }).click();
      await expect(page).toHaveURL(route.path);
      await expect(page).toHaveTitle(`${route.label} · PluginBench`);
      await expect(page.getByRole('heading', { level: 1 })).toHaveText(route.heading);
      await expect(page.getByLabel('Project: PluginBench', { exact: true })).toBeVisible();
      const frame = await page.locator('.fg-site-docs').boundingBox();
      const header = await page.locator('.fg-site-header').boundingBox();
      const main = await page.locator('main').boundingBox();
      expect(header?.width ?? 0).toBeGreaterThan((frame?.width ?? Infinity) * 0.8);
      expect(main?.y ?? 0).toBeGreaterThanOrEqual((header?.y ?? 0) + (header?.height ?? Infinity));
      if (width > 800) {
        const rail = await page.locator('.fg-site-rail').boundingBox();
        expect(main?.x ?? 0).toBeGreaterThanOrEqual((rail?.x ?? 0) + (rail?.width ?? Infinity));
        expect(main?.width ?? 0).toBeGreaterThan(rail?.width ?? Infinity);
      }
      await expectNoOverflow(page);
      for (const region of await page.locator('main .table-scroll').all()) {
        expect(await region.evaluate((element) => element.clientWidth <= innerWidth)).toBe(true);
        // A table that scrolls sideways always has a visible cue above it.
        const scrollsWithoutCue = await region.evaluate((element) => {
          const cue = element.previousElementSibling;
          const cueVisible = cue?.classList.contains('table-cue') && getComputedStyle(cue).display !== 'none';
          return element.scrollWidth > element.clientWidth && !cueVisible;
        });
        expect(scrollsWithoutCue, `${route.path} table under ${await region.getAttribute('aria-labelledby')}`).toBe(false);
        // Row names stay whole: an identifier in a first column never breaks across lines.
        const splitNames = await region.locator('tbody td:first-child code').evaluateAll((codes) => codes
          .filter((code) => !/\s/.test(code.textContent ?? '') && code.getClientRects().length > 1)
          .map((code) => code.textContent));
        if (!await region.evaluate((element) => element.previousElementSibling?.classList.contains('table-cue'))) expect(splitNames).toEqual([]);
      }
    }
    await expect(page.locator('.fg-site-rail')).toHaveAttribute('data-enhanced', 'true');
    if (width <= 800) await page.locator('.fg-site-rail__toggle').click();
    const nav = page.getByRole('navigation', { name: 'Documentation', exact: true });
    await expect(nav.locator('[aria-current="page"]')).toHaveText('Measurement specification');
    await nav.getByRole('group', { name: 'On this page', exact: true }).getByRole('link', { name: 'Requirements', exact: true }).click();
    await expect(page).toHaveURL(/#requirements$/);
    await expect(page.locator('#requirements')).toBeInViewport();
    if (width <= 800) await expect(page.getByRole('dialog')).toHaveCount(0);
  });
}

test('every internal link and heading anchor resolves; document links stay on the site; repository links stay on GitHub', async ({ page }) => {
  const targets = new Map<string, Set<string>>();
  const links: string[] = [];
  for (const route of routes) {
    const response = await page.goto(route.path);
    expect(response?.status()).toBe(200);
    targets.set(new URL(page.url()).pathname, new Set(await page.locator('[id]').evaluateAll((elements) => elements.map(({ id }) => id))));
    links.push(...await page.locator('a[href]').evaluateAll((elements) => elements.map((element) => new URL(element.getAttribute('href') ?? '', location.href).href)));
  }
  for (const href of links) {
    const url = new URL(href, page.url());
    if (url.origin !== new URL(page.url()).origin) continue;
    expect(targets.has(url.pathname), href).toBe(true);
    if (url.hash) expect(targets.get(url.pathname)?.has(decodeURIComponent(url.hash.slice(1))), href).toBe(true);
  }
  // A published document must never be linked as its GitHub source file.
  const publishedSources = documents.map(({ source }) => `https://github.com/filipgutica/pluginbench/blob/main/${source}`);
  for (const route of routes) {
    await page.goto(route.path);
    const bodyLinks = await page.getByRole('main').locator('a[href]').evaluateAll((elements) => elements.map((element) => (element as HTMLAnchorElement).href));
    for (const href of bodyLinks) expect(publishedSources.some((source) => href.split('#')[0] === source), href).toBe(false);
  }
  const documentLinks = [
    { from: '/pluginbench/running-evaluations/', name: 'Configuration reference', href: '/pluginbench/config-reference/', heading: 'Configuration reference' },
    { from: '/pluginbench/reproduce/', name: 'supported coding CLI', href: '/pluginbench/running-evaluations/#supported-coding-clis', heading: 'Evaluation details' },
    { from: '/pluginbench/metrics/', name: 'running evaluations', href: '/pluginbench/running-evaluations/#compare-runs', heading: 'Evaluation details' },
  ];
  for (const { from, name, href, heading } of documentLinks) {
    await page.goto(from);
    const link = page.getByRole('main').getByRole('link', { name, exact: true });
    await expect(link).toHaveAttribute('href', href);
    await link.click();
    await expect(page).toHaveURL(new URL(href, page.url()).href);
    await expect(page.getByRole('heading', { level: 1 })).toHaveText(heading);
    const fragment = new URL(href, page.url()).hash.slice(1);
    if (fragment) await expect(page.locator(`[id="${fragment}"]`)).toBeInViewport();
  }
  await page.goto('/pluginbench/running-evaluations/');
  await expect(page.getByRole('link', { name: 'the starter config', exact: true })).toHaveAttribute('href', 'https://github.com/filipgutica/pluginbench/blob/main/src/pluginbench/evaluation.example.yaml');
  await expect(page.getByRole('link', { name: 'Coverage profiles', exact: true })).toHaveAttribute('href', 'https://github.com/filipgutica/pluginbench/blob/main/docs/coverage-profiles/README.md');
  await expect(page.getByRole('link', { name: 'panel manifest', exact: true })).toHaveAttribute('href', 'https://github.com/filipgutica/pluginbench/blob/main/docs/evidence/deepswe-panel-20261109/manifest.json');
});

test('Config reference renders a readable GFM table that scrolls within the page', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 900 });
  await page.goto('/pluginbench/config-reference/');
  const region = page.getByRole('region', { name: 'Evaluation file', exact: true }).first();
  const table = region.getByRole('table');
  await expect(table.getByRole('columnheader')).toHaveText(['Key path', 'Type', 'Default', 'Required', 'Description']);
  await expect(table.getByRole('cell', { name: 'runner.harness', exact: true })).toBeVisible();
  expect(await region.evaluate((element) => element.scrollWidth > element.clientWidth)).toBe(true);
  await region.scrollIntoViewIfNeeded();
  await region.focus();
  await expect(region).toBeFocused();
  await page.keyboard.press('ArrowRight');
  await expect.poll(() => region.evaluate((element) => element.scrollLeft)).toBeGreaterThan(0);
  // The first column stays in view while the rest of the table scrolls under it.
  const regionBox = await region.boundingBox();
  const keyCell = await table.getByRole('cell', { name: 'runner.harness', exact: true }).boundingBox();
  expect(Math.abs((keyCell?.x ?? Infinity) - (regionBox?.x ?? 0))).toBeLessThan(1);
  await expect(page.getByText('Scroll the table sideways to see every column.').first()).toBeVisible();
  await page.setViewportSize({ width: 1280, height: 900 });
  await expect(page.getByText('Scroll the table sideways to see every column.').first()).toBeHidden();
  await expectNoOverflow(page);
});

test('Home installs from PyPI and copies the command', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 900 });
  await page.goto('/pluginbench/');
  const hero = page.locator('.fg-site-hero');
  await expect(hero.locator('pre')).toHaveText('pipx install pluginbench');
  await expect(hero.getByRole('button', { name: 'Copy code' })).toBeVisible();
  // The command fits without scrolling, at phone or desktop width.
  const scrollsSideways = () => hero.locator('pre').evaluate((element) => element.scrollWidth > element.clientWidth);
  expect(await scrollsSideways()).toBe(false);
  await expect(page.getByRole('main').getByRole('link', { name: 'Full reference', exact: true })).toHaveAttribute('href', 'https://github.com/filipgutica/pluginbench#readme');
  await expectNoOverflow(page);
  await page.setViewportSize({ width: 1280, height: 900 });
  expect(await scrollsSideways()).toBe(false);
});

test('shared project menu, header links, and appearance picker work', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto('/pluginbench/');
  await expect(page.getByRole('navigation', { name: 'Site links' }).getByRole('link')).toHaveText(['GitHub', 'Releases']);
  await expect(page.getByRole('navigation', { name: 'Project links' }).getByRole('link')).toHaveText(['Report an issue', 'Releases']);
  const trigger = page.getByLabel('Project: PluginBench', { exact: true });
  await trigger.click();
  const projects = page.getByRole('navigation', { name: 'Projects', exact: true });
  await expect(projects.getByRole('link')).toHaveText(['annoterm', 'wtree', 'devps', 'PluginBench', 'Workbench', 'Filip Stack', 'UI']);
  await expect(projects.locator('[aria-current="page"]')).toHaveText('PluginBench');
  await page.keyboard.press('Escape');
  await expect(projects).toBeHidden();
  await expect(trigger).toBeFocused();
  await page.getByRole('button', { name: 'Dark theme', exact: true }).click();
  await expect(page.locator('html')).toHaveClass('dark');
  await page.goto('/pluginbench/config-reference/');
  await expect(page.locator('html')).toHaveClass('dark');
  await page.getByRole('button', { name: 'Light theme', exact: true }).click();
  await expect(page.locator('html')).not.toHaveClass('dark');
});

test('Markdown shell commands are command plates: $ on each command, none on continuations, and Copy', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  await page.setViewportSize({ width: 390, height: 900 });
  await page.goto('/pluginbench/running-evaluations/');
  // Scrolling and the drawer re-render the frame island; the mounted blocks must survive both.
  await page.locator('#judging').scrollIntoViewIfNeeded();
  await expect(page.locator('.fg-site-rail')).toHaveAttribute('data-enhanced', 'true');
  await page.locator('.fg-site-rail__toggle').click();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);

  const block = page.locator('.fg-code-block[data-command="true"]').filter({ hasText: 'pluginbench judge prepare' });
  const prompts = await block.locator('.fg-code-block__line').evaluateAll((lines) => lines.map((line) => getComputedStyle(line, '::before').content.includes('$')));
  expect(prompts).toEqual([true, false, false, true, false, true]);
  await block.getByRole('button', { name: 'Copy code' }).click();
  await expect(block.getByRole('status')).toHaveText('Code copied to clipboard.');
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe([
    'pluginbench judge prepare runs/r3=workflow-r3 runs/r2=workflow-main \\',
    '  --tasks task-a,task-b --exclude holdout-a,holdout-b \\',
    '  --criteria review-standard.md --seed 11 --output judging/r3',
    'pluginbench judge run judging/r3 --judge codex:gpt-6-astra:high \\',
    '  --judge claude:claude-opus-5-5:high --jobs 3',
    'pluginbench judge tally judging/r3',
  ].join('\n'));
});

test('Markdown source samples are library code blocks; JSON gets syntax colours', async ({ page }) => {
  await page.goto('/pluginbench/running-evaluations/');
  await expect(page.locator('[data-code-language="yaml"] > .fg-code-block:not([data-command])')).toHaveCount(5);
  const json = page.locator('[data-code-language="json"] .fg-code-block__pre');
  await expect(json.locator('span[style*="--color-syntax-string"]').first()).toBeVisible();
});

test('code blocks and tables are Tab stops only while they scroll', async ({ page }) => {
  const tabStop = (locator: ReturnType<typeof page.locator>) => locator.evaluate((element) => element.getAttribute('tabindex'));
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto('/pluginbench/reproduce/');
  const shortCommand = page.locator('.fg-code-block__pre').filter({ hasText: 'pluginbench compare runs/testem-vanilla' });
  await expect.poll(() => tabStop(shortCommand)).toBeNull();
  await page.goto('/pluginbench/running-evaluations/');
  const narrowTable = page.getByRole('region', { name: 'Measurements, table 1 of 2', exact: true });
  await expect.poll(() => tabStop(narrowTable)).toBeNull();

  await page.setViewportSize({ width: 390, height: 900 });
  const longCommand = page.locator('.fg-code-block__pre').filter({ hasText: 'pluginbench judge prepare' });
  await expect.poll(() => tabStop(longCommand)).toBe('0');
  await page.goto('/pluginbench/config-reference/');
  await expect.poll(() => tabStop(page.getByRole('region', { name: 'Evaluation file', exact: true }))).toBe('0');
});

test('code blocks keep their height when Copy appears, so deep links do not drift', async ({ page, browser }) => {
  const heights = (target: typeof page) => target.locator('main .fg-code-block').evaluateAll((blocks) => blocks.map((block) => block.getBoundingClientRect().height));
  const staticContext = await browser.newContext({ javaScriptEnabled: false, baseURL: 'http://127.0.0.1:4188' });
  const staticPage = await staticContext.newPage();
  await staticPage.goto('/pluginbench/running-evaluations/');
  const before = await heights(staticPage);
  await staticContext.close();
  await page.goto('/pluginbench/running-evaluations/');
  await expect(page.locator('[data-code-language] .fg-code-block__copy')).toHaveCount(9);
  expect(await heights(page)).toEqual(before);
});
