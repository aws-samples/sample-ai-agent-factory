# Observability (unshipped prototype)

This directory is **not invoked by any workshop flow**. It is not referenced
by `contentspec.yaml`, any page under `content/`, or any step in
`code-editor.yaml`'s `SyncWorkshopAssets` document. Workshop participants
never encounter it.

The scripts and templates here are a local-development prototype for a
CloudWatch dashboard on top of the LLM Gateway. Their defaults were last
updated before the Workshop Studio stack-naming rename and will not work
out of the box; you must pass the current stack name explicitly, for
example:

```bash
bash scripts/deploy-dashboard.sh workshop-llm-gateway-stack
```

Decide whether to finish and wire this up or delete the directory before
merging future workshop changes.
