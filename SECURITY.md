# Security

## Supported version

PluginBench is experimental. The latest release and current `main` branch are supported.

## Execution boundary

PluginBench executes model-generated commands. Use only trusted task catalogs, container images, guidance, skill/plugin packages, and repository snapshots.

New coding evaluations use Harbor 0.24.0 and a fresh Docker container for each task, guidance package, and repeat. Containers have CPU and memory limits. PluginBench freezes the task image and complete installed agent image before evaluation; it removes inherited personal instructions before running the pinned native CLI. The host home and sibling run directories are not mounted into the container. Complete declared packages are frozen and installed into private native locations. Synchronous plugin command hooks can execute code with the coding agent's access, including its provider credential. Hook trust is bypassed only for the coding invocation after package verification. Do not evaluate untrusted packages with credentials.

Harbor runs the task's separate native verifier with its supplied trusted tests. PluginBench exports code only after Docker confirms the coding container has stopped. It binds task-defined grading artifacts to the frozen measured submission. Hidden tests and reference solutions are not supplied to the coding container. Shared verifiers, task-authored Compose, multistep tasks, and accelerator environments are currently rejected.

Local collectors and reviews use an OS filesystem boundary: macOS Seatbelt or Linux Bubblewrap. They can access only their job copy, private runtime, selected trusted assets, and read-only toolchain paths. Reviews receive read-only code; trusted scripts and dependencies stay read-only. A missing boundary stops execution.

The OS wrapper enforces Codex reviewers' read-only access. Codex's inner sandbox is disabled inside that wrapper because nested Seatbelt prevents command startup. Packet writes and reads outside the allowed roots remain blocked.

Native coding, grading, and configured coverage commands use Docker resource limits. Local static analysis and reviews share host resources. On macOS, local processes share the host network and process IDs; system/runtime paths remain readable. Linux Bubblewrap has stronger descendant isolation but has not been validated on this macOS development machine. Use a trusted machine; this is not a shared-hosting boundary.

Native coding permits the selected provider's fixed host allowlist while retaining task network restrictions. Custom provider endpoints are unsupported. Native graders retain the task's network policy. PluginBench supplies only the selected credential through a temporary private runtime. It uses empty personal CLI settings, removes that runtime after bounded invocations, and scrubs known secrets from saved artifacts. Raw traces can still contain private task data; inspect them before sharing.

Cleanup removes owned containers, networks, per-trial derived images, temporary homes, and job copies. It checks for remaining Docker resources and records cleanup failures. Shared base and prepared agent images, saved evidence, and baseline cache entries remain. Exported code survives cleanup failures as ungraded evidence. Local process groups are terminated and reaped; on macOS, descendants that create another session can escape this cleanup. A machine restart bypasses normal cleanup. PluginBench does not sweep unowned resources or stale directories automatically.

Never commit `auth.json`, API keys, run directories, or model traces that contain secrets.

## Reporting a vulnerability

Use GitHub's private vulnerability-reporting channel when it is available. Do not include credentials, access tokens, or private model traces in the report, and do not open a public issue for an unpatched vulnerability.
