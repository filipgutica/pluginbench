import { expect, test } from '@playwright/test';
import { expectNoOverflow, routes } from './pages';

for (const width of [320, 390, 720, 801, 1280]) {
  test(`all documents and native navigation work without JavaScript at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto('/pluginbench/');
    for (const route of routes) {
      if (width <= 800) await page.locator('.fg-site-rail__menu > summary').click();
      const nav = page.getByRole('navigation', { name: 'Documentation', exact: true });
      await nav.getByRole('link', { name: route.label, exact: true }).click();
      await expect(page).toHaveURL(route.path);
      await expect(page.getByRole('heading', { level: 1 })).toHaveText(route.heading);
      await expect(page.getByRole('group', { name: 'Color theme' })).toHaveCount(0);
      await expectNoOverflow(page);
    }
    const trigger = page.getByLabel('Project: PluginBench', { exact: true });
    await trigger.click();
    await expect(page.getByRole('navigation', { name: 'Projects', exact: true }).getByRole('link')).toHaveCount(8);
    await trigger.click();
    const railMenu = page.locator('.fg-site-rail__menu');
    const contentsMenu = page.locator('.fg-site-contents__menu');
    const contentsSummary = contentsMenu.locator('summary');
    if (width <= 800) {
      await expect(page.locator('.fg-site-rail + .fg-site-contents')).toHaveCount(1);
      await expect(railMenu).toHaveJSProperty('open', false);
      await expect(contentsMenu).toHaveJSProperty('open', false);
    }
    if (await contentsSummary.isVisible()) await contentsSummary.click();
    const contents = page.getByRole('navigation', { name: 'On this page', exact: true });
    await expect(contents.getByRole('link')).toHaveCount(await page.locator('main h2').count());
    await contents.getByRole('link', { name: 'Requirements', exact: true }).click();
    await expect(page).toHaveURL(/#requirements$/);
    await expect(page.locator('#requirements')).toBeInViewport();
  });
}

test('Home shows the install commands without a dead Copy button', async ({ page }) => {
  await page.goto('/pluginbench/');
  await expect(page.locator('.fg-site-hero pre')).toContainText('pipx install pluginbench');
  await expect(page.getByRole('button', { name: 'Copy code' })).toHaveCount(0);
});

test('deep heading links, GFM tables, and OS dark mode work without JavaScript', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 900 });
  await page.emulateMedia({ colorScheme: 'light' });
  await page.goto('/pluginbench/config-reference/#evaluation-file');
  await expect(page.locator('#evaluation-file')).toBeInViewport();
  await expect(page.getByRole('table').first().getByRole('columnheader')).toHaveText(['Key path', 'Type', 'Default', 'Required', 'Description']);
  const lightBackground = await page.locator('body').evaluate((element) => getComputedStyle(element).backgroundColor);
  await page.emulateMedia({ colorScheme: 'dark' });
  await expect.poll(() => page.locator('body').evaluate((element) => getComputedStyle(element).backgroundColor)).not.toBe(lightBackground);
  await expect.poll(() => page.locator('html').evaluate((element) => getComputedStyle(element).colorScheme)).toBe('dark');
  await expectNoOverflow(page);
});

test('Markdown shell commands render as command plates without a dead Copy button', async ({ page }) => {
  await page.goto('/pluginbench/reproduce/');
  await expect(page.locator('.fg-code-block[data-command="true"]')).toHaveCount(4);
  await expect(page.getByRole('button', { name: 'Copy code' })).toHaveCount(0);
});
