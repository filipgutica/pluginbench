---
name: gateway-log-analysis
description: Analyze gateway event logs and create exact JSON summaries from their status fields.
---

# Gateway log analysis

Read all event rows before you calculate the summary.

Treat `status=000` as a client cancellation. Include it in `cancelled`, not `errors`.

Count each status from the parsed fields. Do not infer a status from the message text.

Write the requested JSON file with no extra keys.
