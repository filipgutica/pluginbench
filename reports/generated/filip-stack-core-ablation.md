# Filip-stack core ablation report

This report compares the 7 tasks scoreable in the baseline, full Filip-stack, and core-skill arms.

| Metric | Baseline | Full bundle | Core bundle |
| --- | ---: | ---: | ---: |
| Tasks passed | 2 | 1 | 3 |
| Pass rate | 28.57% | 14.29% | 42.86% |
| Total tokens | 11251938 | 14726939 | 11708595 |
| Tokens per task | 1607420 | 2103848 | 1672656 |
| Nominal cost USD | 0.8769 | 1.0994 | 0.9196 |
| Cost per task USD | 0.1253 | 0.1571 | 0.1314 |
| Duration per task (s) | 460.2 | 471.3 | 526.7 |

## Comparisons

### Core vs baseline

- Reference wins: 0
- Core wins: 1
- Unchanged: 6
- Pass-rate lift: 14.29 percentage points
- Token overhead: 456657 (4.06%)
- Nominal cost overhead: $0.0427 (4.87%)
- Duration overhead: 66.5 seconds per task

### Core vs full

- Reference wins: 0
- Core wins: 2
- Unchanged: 5
- Pass-rate lift: 28.57 percentage points
- Token overhead: -3018344 (-20.50%)
- Nominal cost overhead: -$0.1798 (-16.36%)
- Duration overhead: 55.4 seconds per task

## Task outcomes

| Task | Baseline | Full bundle | Core bundle |
| --- | ---: | ---: | ---: |
| `astropy__astropy-13398` | fail | fail | fail |
| `django__django-16560` | pass | pass | pass |
| `pydata__xarray-6992` | fail | fail | fail |
| `pylint-dev__pylint-4551` | fail | fail | fail |
| `pytest-dev__pytest-5787` | fail | fail | pass |
| `sphinx-doc__sphinx-7590` | fail | fail | fail |
| `sympy__sympy-13878` | pass | fail | pass |
