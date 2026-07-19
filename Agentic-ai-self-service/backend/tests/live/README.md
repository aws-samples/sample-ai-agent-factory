# Live AWS scripts

Scripts here deploy **real AWS resources** (AgentCore runtimes, IAM roles, S3
objects) and are run manually — they are intentionally NOT named `test_*.py`
so pytest never collects them.

```bash
cd backend
PYTHONPATH=src python tests/live/e2e_live_invocation.py
```

For the pytest-managed integration suite (also live, but fixture-guarded and
skipped without `API_GATEWAY_URL`), see `tests/integration/`.
