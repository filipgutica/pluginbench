# Security

## Supported version

PluginBench is experimental. The latest release and current `main` branch are supported.

## Execution boundary

PluginBench executes model-generated commands against local task workspaces. Use only trusted task catalogs, text guidance, and repository snapshots.

Implementation runs use a local Codex or Claude process with write-capable tools, disabled user configuration, and fresh runtime homes. Reviews use read-only mode or restricted tools. This isolates supplied instructions; it is not operating-system containment. Use disposable tasks on a trusted machine. This execution path is not suitable for shared hosting.

PluginBench copies only Codex authentication into a temporary private home. Claude uses a single selected credential with private settings and a restricted reviewer tool set. The primary runner removes that home after each bounded invocation, including failures and timeouts.

DeepSWE tasks are adapted to local source snapshots with separate hidden verification. Hidden tests and reference solutions are excluded from the supplied workspace; local execution does not prevent a model from accessing the host filesystem.

Never commit `auth.json`, API keys, run directories, or model traces that contain secrets.

## Reporting a vulnerability

Use GitHub's private vulnerability-reporting channel when it is available. Do not include credentials, access tokens, or private model traces in the report, and do not open a public issue for an unpatched vulnerability.
