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
