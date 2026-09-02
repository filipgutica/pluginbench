# pluginbench strict report

This companion report excludes `scikit-learn__scikit-learn-25102` from strict paired and efficiency claims because the baseline provider reported 1136.982 seconds of model latency, exceeding the configured 900-second timeout. Both arms passed that excluded task. The canonical `report.json` and `report.md` mark the baseline task unscoreable and remain reproducible with `pluginbench report`; the original pre-guard output is preserved in `raw-report.json` and `raw-report.md`.

| Metric | Baseline | Filip-stack |
| --- | ---: | ---: |
| Comparable tasks passed | 2/7 | 1/7 |
| Pass rate | 28.57% | 14.29% |
| Total tokens | 11,251,938 | 14,726,939 |
| Tokens per task | 1,607,420 | 2,103,848 |
| Nominal cost | $0.8769 | $1.0994 |
| Cost per task | $0.1253 | $0.1571 |
| Duration per task | 460.2s | 471.3s |

## Paired comparison

- Baseline wins: 1
- Treatment wins: 0
- Unchanged tasks: 6
- Pass-rate lift: -14.29 percentage points
- Token overhead: +3,475,001 (+30.88%)
- Nominal cost overhead: +$0.2225 (+25.37%)
- Duration overhead: +11.1 seconds per task

The score difference came from `sympy__sympy-13878`: baseline passed and Filip-stack failed. `django__django-16560` passed in both arms. All other included tasks failed in both arms.
