---
description: "Diagnose an error I paste (azd up output, script error, app error) using the accelerator's own docs"
name: "why-is-this-failing"
argument-hint: "paste the error text, or describe what is going wrong"
agent: "accelerator-troubleshooter"
tools: ["accelerator-troubleshooter/*"]
---
Diagnose this problem with the `diagnose` tool, passing my text as `issue` exactly as written (do not summarise it first):

${input:problem:paste the error or describe the problem}

Then answer with: the **most likely cause** and its status/confidence exactly as the tool labelled them, the evidence and
the doc path with line number, what is still missing, and the smallest safe fix with its category (🟢 / 🟡 / 📘 / 🔴).
Never claim anything was fixed or run unless the tool output shows it.
