import { expect, test as base } from '@playwright/test';
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
      const nav = page.getByRole('navigation', { name: 'On this page', exact: true });
      await expect(nav.getByRole('group', { name: 'Documentation', exact: true }).getByRole('link')).toHaveText(routes.map(({ label }) => label));
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
      for (const region of await page.getByRole('region', { name: 'Scrollable table' }).all()) {
        expect(await region.evaluate((element) => element.clientWidth <= innerWidth)).toBe(true);
      }
    }
    await expect(page.locator('.fg-site-rail')).toHaveAttribute('data-enhanced', 'true');
    if (width <= 800) await page.locator('.fg-site-rail__toggle').click();
    const nav = page.getByRole('navigation', { name: 'On this page', exact: true });
    await expect(nav.locator('[aria-current="page"]')).toHaveText('Measurement specification');
    await nav.getByRole('link', { name: 'Requirements', exact: true }).click();
    await expect(page).toHaveURL(/#requirements$/);
    await expect(page.locator('#requirements')).toBeInViewport();
    if (width <= 800) await expect(page.getByRole('dialog')).toHaveCount(0);
  });
}

test('every internal link and heading anchor resolves; repository links stay on GitHub', async ({ page }) => {
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
  await page.goto('/pluginbench/running-evaluations/');
  await expect(page.getByRole('link', { name: 'the starter config', exact: true })).toHaveAttribute('href', 'https://github.com/filipgutica/pluginbench/blob/main/src/pluginbench/evaluation.example.yaml');
  await expect(page.getByRole('link', { name: 'Coverage profiles', exact: true })).toHaveAttribute('href', 'https://github.com/filipgutica/pluginbench/blob/main/docs/coverage-profiles/README.md');
  await expect(page.getByRole('link', { name: 'panel manifest', exact: true })).toHaveAttribute('href', 'https://github.com/filipgutica/pluginbench/blob/main/docs/evidence/deepswe-panel-20261109/manifest.json');
});

test('Config reference renders a readable GFM table that scrolls within the page', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 900 });
  await page.goto('/pluginbench/config-reference/');
  const region = page.getByRole('region', { name: 'Scrollable table' }).first();
  const table = region.getByRole('table');
  await expect(table.getByRole('columnheader')).toHaveText(['Key path', 'Type', 'Default', 'Required', 'Description']);
  await expect(table.getByRole('cell', { name: 'runner.harness', exact: true })).toBeVisible();
  expect(await region.evaluate((element) => element.scrollWidth > element.clientWidth)).toBe(true);
  await region.scrollIntoViewIfNeeded();
  await region.focus();
  await expect(region).toBeFocused();
  await page.keyboard.press('ArrowRight');
  await expect.poll(() => region.evaluate((element) => element.scrollLeft)).toBeGreaterThan(0);
  await expectNoOverflow(page);
});

test('shared project menu, header links, and appearance picker work', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto('/pluginbench/');
  await expect(page.getByRole('navigation', { name: 'Site links' }).getByRole('link')).toHaveText(['GitHub', 'Releases']);
  await expect(page.getByRole('navigation', { name: 'Project links' }).getByRole('link')).toHaveText(['Issues', 'Releases']);
  const trigger = page.getByLabel('Project: PluginBench', { exact: true });
  await trigger.click();
  const projects = page.getByRole('navigation', { name: 'Projects', exact: true });
  await expect(projects.getByRole('link')).toHaveText(['annoterm', 'wtree', 'devps', 'Workbench', 'Filip Stack', 'UI', 'PluginBench']);
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
