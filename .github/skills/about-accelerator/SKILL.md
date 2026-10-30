---
name: about-accelerator
description: Tell me about this accelerator - its name, a 2-3 line description from its own docs, Azure services, and which docs I search. Use for "what is this?", "about this accelerator", or when starting out.
argument-hint: optional accelerator id, e.g. content-processing
---

Call the `about_accelerator` tool of the accelerator-troubleshooter MCP server (pass `accelerator` only if the user named one).

Reply in a few short bullets: the accelerator's **name**, **what it is in 2-3 lines** (use the tool's description as
written and cite its `README.md:line`), the **Azure services** it uses, the **deployment flavors**, and the **docs you
search first**. Then end with "Ask next:" offering the other three questions: preflight (before deploying), health check
(after deploying), and why is this failing (paste an error).

If the tool says the repository was not found, say so and give the upstream repository it names.
