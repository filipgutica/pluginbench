# Releasing

Releases are cut by the `Release` workflow (`.github/workflows/release.yml`) with release-please.

## How a release happens

1. Every push to `main` updates one open pull request, titled `chore(main): release <version>`. It holds the version bump in `pyproject.toml`, `src/pluginbench/__init__.py`, and `.release-please-manifest.json`, and the new `CHANGELOG.md` section, written from the commit subjects since the last release.
2. The version follows the commits: before 1.0.0, a `feat` or a breaking change raises the minor version, and anything else raises the patch version. To choose a version by hand, add a commit whose message body has the line `Release-As: <version>`.
3. Run CI on that pull request by hand. It is opened with the workflow token, which starts no workflow:

   ```sh
   gh workflow run CI --ref release-please--branches--main
   ```

4. Merging the pull request is the decision to release. The workflow then tags `v<version>`, creates the GitHub release, builds the package from that tag, checks it with `scripts/check_dist.py`, and waits for approval in the `pypi` environment before it uploads to PyPI.

Commit subjects become the changelog, so write them for a user: `fix: …`, `feat: …`, `docs: …`.

## One-time setup

The workflow cannot publish until all three exist:

- **The `pypi` environment** in the repository settings, with a required reviewer. Without the reviewer, GitHub creates the environment on first use and the upload runs unattended.
- **A trusted publisher on PyPI** for project `pluginbench`: owner `filipgutica`, repository `pluginbench`, workflow `release.yml`, environment `pypi`. No API token is stored anywhere.
- **Read and write workflow permissions**, and "Allow GitHub Actions to create and approve pull requests", in the repository's Actions settings.
