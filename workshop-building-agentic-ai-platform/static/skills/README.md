# Agent skills

`SKILL.md` documents that `workshop-tools-stack.yaml` seeds into the MCP Registry so
that participants see something under **Skills** in the Registry UI before they
register anything themselves.

## Why these live in the repository rather than being fetched from upstream

The Registry's `POST /api/skills` endpoint requires `skill_md_url` and *fetches* it
during registration — a URL that returns anything other than `200` is rejected with
`400 Invalid SKILL.md URL`. Verified against a live registry:

| `skill_md_url` | Result |
|---|---|
| omitted | `422` — `Field required` |
| `skill_md` body inline instead | `422` — `skill_md_url` is still required |
| host that does not resolve | `400` — failed SSRF validation |
| real host, path returns `404` | `400 Invalid SKILL.md URL: HTTP 404` |
| any public host returning `200` | `201 Created` |

So the URL cannot be dropped, cannot be replaced with inline content, and cannot
point at a path that does not exist yet. It has to be a live, publicly fetchable,
Amazon-owned URL — which is why these files are versioned here and referenced from
the workshop's own published tree in `aws-samples/sample-ai-agent-factory` rather
than from a third-party organization.

## Publish ordering

`workshop-tools-stack.yaml` points at
`https://raw.githubusercontent.com/aws-samples/sample-ai-agent-factory/main/workshop-building-agentic-ai-platform/static/skills/<name>/SKILL.md`.
That URL resolves only once this directory has been published to the public
repository. Until then the two seed registrations fail — non-fatally: the
registration handler catches the error, prints `Failed`, and continues, and no
content page, notebook, or verification step references either skill. Publish this
directory together with any change to the URLs above.
