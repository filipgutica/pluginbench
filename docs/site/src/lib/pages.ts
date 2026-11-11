export const pages = [
  {
    slug: '',
    title: 'Home',
    source: 'docs/site/src/content/home.md',
    description: 'Compare coding agents with and without instructions, skills, and plugins.',
  },
  {
    slug: 'running-evaluations',
    title: 'Running evaluations',
    source: 'docs/running-evaluations.md',
    description: 'Choose inputs, configure treatments, run evaluations, and compare results.',
  },
  {
    slug: 'config-reference',
    title: 'Config reference',
    source: 'docs/config-reference.md',
    description: 'Evaluation and gates fields, types, defaults, and descriptions.',
  },
  {
    slug: 'metrics',
    title: 'Metrics',
    source: 'docs/metrics.md',
    description: 'Measurement definitions, units, and exact metric names for gates.',
  },
  {
    slug: 'reproduce',
    title: 'Reproduce a result',
    source: 'docs/reproduce.md',
    description: 'Pin inputs, preserve receipts, and reproduce a public evaluation.',
  },
  {
    slug: 'measurement-spec',
    title: 'Measurement specification',
    source: 'docs/measurement-spec.md',
    description: 'The specification for correctness, code, test, and cost measurements.',
  },
];

export const pageHref = (slug: string) => `${import.meta.env.BASE_URL}${slug ? `${slug}/` : ''}`;
