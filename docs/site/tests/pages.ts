import { expect, type Page } from '@playwright/test';

export const routes = [
  { path: '/pluginbench/', label: 'Home', heading: 'Compare the guidance you give coding agents.' },
  { path: '/pluginbench/running-evaluations/', label: 'Running evaluations', heading: 'Evaluation details' },
  { path: '/pluginbench/config-reference/', label: 'Config reference', heading: 'Configuration reference' },
  { path: '/pluginbench/metrics/', label: 'Metrics', heading: 'Metrics glossary' },
  { path: '/pluginbench/reproduce/', label: 'Reproduce a result', heading: 'Reproduce an evaluation' },
  { path: '/pluginbench/measurement-spec/', label: 'Measurement specification', heading: 'Measurement specification' },
];

export const expectNoOverflow = async (page: Page) => {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
};
