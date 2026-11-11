import { expect, test } from '@playwright/test';
import { expectNoOverflow, routes } from './pages';

for (const width of [320, 390, 720, 801, 1280]) {
  test(`all documents and native navigation work without JavaScript at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto('/pluginbench/');
    for (const route of routes) {
      if (width <= 800) await page.locator('.fg-site-rail__menu > summary').click();
      const nav = page.getByRole('navigation', { name: 'On this page', exact: true });
      await nav.getByRole('link', { name: route.label, exact: true }).click();
      await expect(page).toHaveURL(route.path);
      await expect(page.getByRole('heading', { level: 1 })).toHaveText(route.heading);
      await expect(page.getByRole('group', { name: 'Color theme' })).toHaveCount(0);
      await expectNoOverflow(page);
    }
    const trigger = page.getByLabel('Project: PluginBench', { exact: true });
    await trigger.click();
    await expect(page.getByRole('navigation', { name: 'Projects', exact: true }).getByRole('link')).toHaveCount(7);
    await trigger.click();
    if (width <= 800) await page.locator('.fg-site-rail__menu > summary').click();
    await page.getByRole('navigation', { name: 'On this page', exact: true }).getByRole('link', { name: 'Requirements', exact: true }).click();
    await expect(page.locator('#requirements')).toBeInViewport();
  });
}

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
