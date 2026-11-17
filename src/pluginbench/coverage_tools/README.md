# Offline coverage distributions

PluginBench ships these artifacts and verifies their SHA-256 pins in
`measurement_tools.py` before checking the container runtime. Measurement never
fetches or installs packages. The c8 archive contains its complete production
`node_modules` tree, package manifest, integrity lock, and upstream licence files.
It omits executable symlinks; invoke its CLI with `node`.

| Tool | Artifact | Runtime | Upstream licence |
| --- | --- | --- | --- |
| coverage.py 7.6.12 | `coverage-7.6.12-py3-none-any.whl` | Python 3.9+ | Apache-2.0 |
| c8 10.1.3 | `c8-10.1.3.tar.gz` | Node 18+ | ISC; dependencies: MIT, ISC, BSD-3-Clause, BlueOak-1.0.0 |

The coverage wheel is the unmodified PyPI artifact. Its hash was checked against
<https://pypi.org/pypi/coverage/7.6.12/json>. It uses the Python tracer without a
platform-specific C extension. Some advanced coverage.py concurrency and plugin
features require that extension and are outside this distribution's scope.

The c8 bundle was assembled with `npm install --ignore-scripts --omit=dev
--save-exact c8@10.1.3` in an empty directory. The adjacent `c8-package.json` and
`c8-package-lock.json` preserve the manifest and every dependency's npm integrity
pin. To rebuild, copy those files to an empty directory as `package.json` and
`package-lock.json`, then run `npm ci --ignore-scripts --omit=dev`. Archive the
regular files with their relative paths, including licences. Symlinks are not
needed by the explicit Node entry point. A rebuilt archive can have different
archive metadata; verify its contents and update its SHA-256 deliberately.

Changing a release or its dependency bundle changes the measurement profile's
identity. Changes to the provisioning behavior follow the `measurement_tools`
component version in `components.py`. Coding launch identity stays unchanged.


## Runtime entry points

`launch-coverage.py`, `coverage-config.ini`, `launch-c8.cjs`, and `c8-config.json` are PluginBench code
bound by the `measurement_tools` component version. They are staged beside the
selected distribution. Use the documented launchers, rather than the distributions'
raw entry points. Provisioner version 5 reports in a fresh container with a fresh artifact extraction,
pins the reporting interpreter/configuration/data paths, includes the complete
profile source population, ignores source maps, and disables exclusion hints. The staged
`source-files.json` comes from the frozen snapshot and profile scope, not task
configuration. It limits report output without exposing tool options. The c8 process receives empty `NODE_OPTIONS`,
`NODE_PATH`, and `EXPERIMENTAL_MONOCART` before Node starts.

## eastasianwidth 0.2.0 notice

The npm tarball omits its MIT notice. The bundle now includes the maintained
`MIT-LICENSE.txt` and `LICENSE-NOTES.md` under `node_modules/eastasianwidth`.
The adjacent `eastasianwidth-LICENSE` and `eastasianwidth-NOTICE` are exact copies,
retrieved 2026-11-17 from the maintainer's repository:

- [MIT notice](https://github.com/komagata/eastasianwidth/blob/master/MIT-LICENSE.txt), Git blob `d176e6e6f3c5918d4b1709e854b9149b06d84bea`.
- [Licensing notes](https://github.com/komagata/eastasianwidth/blob/master/LICENSE-NOTES.md), Git blob `ad971a6f5a606838d9f1082452080acb0e6b4e73`.

The notes identify 0.2.0's source commit as `b89f04d44dc786885615e94cd6e2ba1ef7866fa4`
and explain how the maintained notice applies to that historical package.
Package source and dependency locks are unchanged. The archive was repacked with
sorted paths, mode 0644, zero timestamps, and gzip mtime 0. Its new SHA-256 creates
a new artifact identity. Retain these two notices when rebuilding the bundle.
