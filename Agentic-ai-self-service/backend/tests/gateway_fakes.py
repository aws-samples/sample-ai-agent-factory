"""A MagicMock control client that behaves like a gateway under UpdateGateway.

Every gateway writer now waits for its own update to read back (F-66e), so a fake
whose GetGateway never changes is a stale read-back, and the writer correctly refuses
it. ``applying_updates`` makes the fake READY and makes each update land as the full
replace it is: only the identity fields survive, everything else is what was sent.
"""

from __future__ import annotations

_IDENTITY = ("gatewayId", "gatewayArn", "gatewayUrl", "status")


def applying_updates(ctrl):
    """Start from ``ctrl.get_gateway.return_value``; return ``ctrl``."""
    state = {"detail": {"status": "READY", **dict(ctrl.get_gateway.return_value or {})}}

    def _get(**_kw):
        return dict(state["detail"])

    def _update(**request):
        kept = {k: state["detail"][k] for k in _IDENTITY if k in state["detail"]}
        state["detail"] = {**kept, **{k: v for k, v in request.items() if k != "gatewayIdentifier"}}
        return {}

    ctrl.get_gateway.side_effect = _get
    ctrl.update_gateway.side_effect = _update
    return ctrl
