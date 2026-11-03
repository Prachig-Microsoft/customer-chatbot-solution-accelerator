---
name: prove-chat-works
description: Prove the deployed chat really works - sends ONE real chat message with your own sign-in, checks the model replies, and deletes the test session. Use for "does chat really work?", "send a test message", or after a healthy health check.
argument-hint: optional resource group name, e.g. rg-mydeployment
---

Health probes only prove the apps are alive. This sends one real message through the same path a user takes.

1. Tell the user exactly what will happen and ask a plain yes/no: "I will send ONE real chat message to your chat API
   with your own sign-in. It uses a little model quota and I delete the test chat session afterwards. Nothing else is
   changed. Send it?"
2. **Only after yes**, call `chat_test` with `user_approved=true` (pass `resource_group` if the user gave one, and
   `client_id` if the report says it is needed). Never call it without a yes.
3. Answer in a few bullets: whether a real reply came back and how fast, or **where it broke** (sign-in, missing
   configuration, Cosmos DB firewall, role denied, model deployment missing, throttling, timeout) using the tool's labels
   exactly. If a test chat session may still exist, say so and give its id.
4. If the tool says it could not get a sign-in token, show its "how to fix" steps as a short numbered list. Never ask the
   user to paste a token into chat, and never show or store one.
5. End with "Sources": 🔬 `chat.e2e` and the other checks used, 📘 any doc cited as `path:line`.
