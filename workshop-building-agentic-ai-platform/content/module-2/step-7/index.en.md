---
title: "Cleanup"
weight: 37
---

::alert[Skip this step if continuing to Module 3 — all resources created in Module 2 are reused by Module 3 and Module 4. Only read this page if you are stopping after Module 2 or want to understand what happens next.]{type="info"}

**There is nothing to clean up at the end of Module 2.** Every resource you created — the virtual keys, the two teams, the guardrails, and the SSM parameters storing the gateway URL and key — is reused by Module 3 and Module 4. Tearing them down here would force you to re-run `setup_keys.py` in the very next module.

::alert[The workshop has **one** cleanup step, and it lives at the end. When you finish the full workshop, follow [Workshop Cleanup](../../cleanup/) to tear down all four stacks (`workshop-llm-gateway-stack`, `workshop-registry-stack`, `workshop-tools-gateway-stack`, `workshop-agentcore-stack`) together. If you only completed Module 2 and want to stop here, running that same page is still the right way to release the resources.]{type="info"}

Proceed to [Module 3a — OSS MCP Registry & Tools Gateway](../../module-3a/).
