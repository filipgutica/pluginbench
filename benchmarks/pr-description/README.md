# Historical PR-description fixture

Does supplied writing guidance reduce the PR-description corrections you need to make?

The task writes one `PR.md` for [devps PR #10](https://github.com/filipgutica/devps/pull/10): a real picker behavior change. Both arms get the same pinned diff, selected source, and recorded CI facts. The original PR body and personal formatting rules are absent from the task inputs. Add the frozen `preference-guide.md` to the guided arm only.

Prepare once from the repository root:

```sh
python3 benchmarks/pr-description/prepare.py
```

This fixture is retained for verifier regression tests and earlier studies. The current `pluginbench run` interface does not accept it. Preparation accepts `--repo /path/to/devps` to reuse a clone without modifying it; otherwise it clones the public repository. Generated inputs are ignored by Git.

The external verifier checks preferred headings, ordered feature-verification actions, routine CI repetition, untouched source inputs, and a few unsupported execution claims. It records words, paragraphs and command lines without a word-count pass limit. A passing result still needs blind review for readable prose, excess detail, valid commands and accurate claims. Report these preference and review results separately from coding metrics.

Earlier studies used repair feedback after rejected drafts. Those measured verifier-driven repair calls, not real human review churn. The current public interface has no repair rounds.

Before model runs, check the prepared verifier with known good and bad drafts:

```sh
python3 benchmarks/pr-description/calibrate.py benchmarks/pr-description/generated/tasks.json
```

`source.json` pins the base/head commits, historical check identities and timestamps. Preparation hashes the patch, captured facts and preference guide. The upstream MIT license is included in the fixture. CI success is historical evidence; this task performs no fresh source checks.
