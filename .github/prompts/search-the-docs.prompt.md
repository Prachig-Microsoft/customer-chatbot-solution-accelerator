---
description: "Search the accelerator's troubleshooting and deployment docs and cite the exact file and line"
name: "search-the-docs"
argument-hint: "an error code or topic, e.g. InsufficientQuota"
agent: "accelerator-troubleshooter"
tools: ["accelerator-troubleshooter/*"]
---
Use the `search_docs` tool for: ${input:topic:error code or topic}

Summarise what the accelerator's own documentation says, quote the resolution steps briefly, and cite every statement with
its `path:line`. If nothing relevant is found, say so instead of answering from general knowledge.
