# Upstream provenance

- Source: `https://github.com/obra/superpowers/tree/v6.2.0/skills/systematic-debugging`
- Pin: `6.2.0`
- License: MIT. See `../../../THIRD_PARTY_NOTICES.md`.

## Retained ideas

- Reproduce before a fix.
- Trace data and state to the causal boundary.
- Test one hypothesis at a time.
- Use a focused regression test for the correction when feasible.
- Reassess the architecture after repeated failed fixes.

## Local changes

- Separate read-only investigation from authorized implementation.
- Apply the repository's local CI reproduction rule.
- Route corrections through the workflow implementation and review cycle.
- Replace mandatory Superpowers dependencies with local workflow skills.

## Update policy

Review upstream changes manually. Do not merge upstream text automatically.
