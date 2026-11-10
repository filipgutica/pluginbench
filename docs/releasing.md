# Releasing

Releases are cut by the `Release` workflow (`.github/workflows/release.yml`) with release-please.

## How a release happens

1. Every push to `main` updates one open pull request, titled `chore(main): release <version>`. It holds the version bump in `pyproject.toml`, `src/pluginbench/__init__.py`, and `.release-please-manifest.json`, and the new `CHANGELOG.md` section, written from the commit subjects since the last release.
2. The version follows the commits: before 1.0.0, a `feat` or a breaking change raises the minor version, and a `fix` or another change that appears in the changelog raises the patch version. Commits that the changelog hides, such as `ci`, `test`, and `chore`, open no release on their own. To choose a version by hand, add a commit whose message body has the line `Release-As: <version>`.
3. Start CI on that pull request. It is opened and updated with the workflow token, so GitHub creates its CI runs in an approval-required state and shows a banner in the merge box: select **Approve workflows to run** (this needs write access). Do this again after each update, so the checks belong to the last commit. If no banner appears, close and reopen the pull request.
4. Merging the pull request is the decision to release. The workflow then tags `v<version>`, creates the GitHub release, builds the package from that tag, checks it with `scripts/check_dist.py`, and uploads it to PyPI from the `pypi` environment.

Commit subjects become the changelog, so write them for a user: `fix: …`, `feat: …`, `docs: …`.

## One-time setup

Do all three before the first release pull request is merged. The workflow does not check them.

- **The `pypi` environment** in the repository settings, with a required reviewer. The workflow cannot tell whether the reviewer is there: if the environment is missing, GitHub creates it with no protection, and an environment with no reviewer lets the upload run unattended.
- **A pending trusted publisher on PyPI** (account settings, Publishing): project `pluginbench`, owner `filipgutica`, repository `pluginbench`, workflow `release.yml`, environment `pypi`. No API token is stored anywhere. A pending publisher does not reserve the name: the project exists only after the first upload, and until then another account can take the name.
- **"Allow GitHub Actions to create and approve pull requests"** in the repository's Actions settings. The default workflow permissions can stay read-only, because each job in the workflow declares what it needs.
