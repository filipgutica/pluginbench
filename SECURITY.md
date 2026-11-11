# Security

Report vulnerabilities through [GitHub private vulnerability reporting](https://github.com/filipgutica/pluginbench/security/advisories/new) on this repository.
Do not open a public issue for an unpatched vulnerability or include credentials or private model traces.
Reports for the latest release and current `main` branch are in scope.

PluginBench runs untrusted agent code inside Docker.
Security issues in container isolation, credential handling, package admission, artifact validation,
and local collectors or reviews are in scope.
It relies on Docker container boundaries and resource limits, separate native verifiers,
and macOS Seatbelt or Linux Bubblewrap for local collectors and reviews.
See [execution and isolation](docs/running-evaluations.md#execution-and-isolation) for the execution boundary and its limits.
Use a trusted machine and trusted task images and packages; this is not a shared-hosting boundary.
Inspect saved evidence before sharing it because traces can contain private task data.
