# Security

## Supported version

PluginBench is an experimental proof of concept. Only the current `main` branch is supported.

## Execution boundary

PluginBench executes model-generated commands against local task workspaces. Use only trusted task catalogs, skill bundles, and repository snapshots.

The SWE-bench runtime uses a privileged outer Docker container. It grants `SYS_ADMIN` and disables the outer AppArmor and seccomp profiles.

The inner Codex sandbox applies the configured file and network restrictions. This design is not suitable for a shared or hosted service.

PluginBench copies the configured Codex `auth.json` into a temporary isolated home. It removes that home after each bounded execution batch.

Never commit `auth.json`, API keys, run directories, or model traces that contain secrets.

## Known dependency advisories

The pinned `promptfoo@0.122.2` release currently reports high-severity advisories in optional or transitive dependencies.

The current upstream release does not remove all reported advisories. PluginBench keeps the tested pin and does not suppress the audit result.

Run this command before each release:

```bash
npm audit --omit=dev
```

Review each advisory against the local execution path. Update Promptfoo when upstream publishes a compatible fix.

## Reporting a vulnerability

Use GitHub's private vulnerability-reporting channel when it is available. Do not include credentials, access tokens, or private model traces in the report, and do not open a public issue for an unpatched vulnerability.
