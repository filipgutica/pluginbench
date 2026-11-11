export const home = {
  slug: '',
  title: 'Home',
  description: 'Compare coding agents with and without instructions, skills, and plugins.',
};

// Rail titles match each document's H1.
export const documents = [
  {
    slug: 'running-evaluations',
    title: 'Evaluation details',
    source: 'docs/running-evaluations.md',
    description: 'Choose inputs, configure treatments, run evaluations, and compare results.',
  },
  {
    slug: 'config-reference',
    title: 'Configuration reference',
    source: 'docs/config-reference.md',
    description: 'Evaluation and gates fields, types, defaults, and descriptions.',
  },
  {
    slug: 'metrics',
    title: 'Metrics glossary',
    source: 'docs/metrics.md',
    description: 'Measurement definitions, units, and exact metric names for gates.',
  },
  {
    slug: 'reproduce',
    title: 'Reproduce an evaluation',
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

export const pages = [home, ...documents];

export const pageHref = (slug: string) => `${import.meta.env.BASE_URL}${slug ? `${slug}/` : ''}`;
