"""AgentCore Workflow Platform Backend."""

from app.logging_config import configure_logging

# Called here, in the package __init__, rather than in each Lambda entrypoint. There
# are twenty: ``lambda_handler``, ``stream_handler``, the cfn provider's ``handler``,
# and one per step handler -- verified against the deployed functions, whose Handler
# values are ``src/app/step_handlers/<name>_step.handler`` for all 17 of them. Adding
# the call to each would be twenty chances to forget and a twenty-first the next time
# a step is added. Importing any module in this package runs this first, so no
# entrypoint can be packaged without it. See ``app.logging_config`` for why it is
# needed at all -- without it every ``logger.info`` in the backend is silent in
# Lambda, which was measured, not assumed.
configure_logging()
