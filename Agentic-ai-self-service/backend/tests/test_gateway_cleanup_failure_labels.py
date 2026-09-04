"""The abort-cleanup log line reports resource *kinds*, never message bodies.

Background. When `deploy_gateway` fails partway it runs `cleanup_gateway_resources`
and logs whatever came back. Those messages are built from `client_info` — which
also carries `client_secret` — and from raw exception text, and a botocore
`ParamValidationError` echoes the failing call's parameters. So the messages are
not a safe thing to interpolate into a log line, and CodeQL reports exactly that
as `py/clear-text-logging-sensitive-data`.

`classify_cleanup_failures` maps them to constants from a fixed vocabulary. That
only works while the vocabulary keeps up with the messages the cleanup path can
actually emit, and nothing about adding a new `cleanup_log.append(f"... error ...")`
would prompt anyone to update it. So the second test below reads the source and
fails when a failure message exists that the vocabulary cannot name — a
regression that would otherwise surface only as "unclassified" in a log nobody
reads until an orphan turns up.
"""

from __future__ import annotations

import pathlib
import re
import sys

sys.path.insert(0, "src")

from app.services.gateway_deployer import (  # noqa: E402
    _CLEANUP_FAILURE_KINDS,
    classify_cleanup_failures,
)

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "app" / "services" / "gateway_deployer.py"


class TestClassification:
    def test_it_names_the_kind_without_echoing_the_body(self):
        secret = "sk-super-secret-value"
        kinds = classify_cleanup_failures([f"Cognito cleanup error: ParamValidationError {secret}"])
        assert kinds == ["cognito-pool"]
        # The point of the exercise: nothing from the input survives into the output.
        assert secret not in "".join(kinds)

    def test_the_more_specific_label_wins(self):
        """'Custom tool Lambda delete error' also contains 'Lambda delete error',
        and 'Gateway IAM role cleanup error' also contains 'IAM role cleanup
        error'. Ordering in the vocabulary is what disambiguates, so a reorder
        that looks harmless would silently mislabel."""
        assert classify_cleanup_failures(["Custom tool Lambda delete error: x"]) == ["custom-tool-lambda"]
        assert classify_cleanup_failures(["Gateway IAM role cleanup error: x"]) == ["gateway-iam-role"]

    def test_kinds_are_deduped_and_sorted(self):
        assert classify_cleanup_failures(
            [
                "Target cleanup error: a",
                "Target cleanup error: b",
                "Gateway delete error: c",
            ]
        ) == ["gateway", "gateway-target"]

    def test_an_unknown_message_is_counted_not_dropped(self):
        """Silently returning [] would report 'left resources behind ()' and read
        as a formatting bug rather than a gap in the vocabulary."""
        assert classify_cleanup_failures(["Something nobody has labelled yet: boom"]) == ["unclassified"]

    def test_no_messages_means_no_kinds(self):
        assert classify_cleanup_failures([]) == []


class TestTheVocabularyIsExhaustive:
    def _failure_messages(self) -> list[str]:
        """Every f-string literal in gateway_deployer that reads as a cleanup
        failure. Matches the source rather than a transcription of it, so this
        cannot pass against a stale copy of the list."""
        src = _SRC.read_text()
        # The literal prefix of each f-string, up to its first interpolation.
        # Covers both ways a cleanup message is produced: appended to cleanup_log,
        # or returned from a per-resource helper for the caller to append.
        literals = re.findall(r'(?:append\(|return (?:True|False), |return )f"([^"{]*)', src)
        return [text for text in literals if "error" in text.lower()]

    def test_every_failure_message_the_source_can_emit_is_named(self):
        unnamed = [
            text
            for text in self._failure_messages()
            if classify_cleanup_failures([text + "detail"]) == ["unclassified"]
        ]
        assert not unnamed, (
            "gateway_deployer can emit cleanup failure messages that "
            f"_CLEANUP_FAILURE_KINDS cannot name: {unnamed}. Add a needle -> kind "
            "entry for each, most-specific first."
        )

    def test_the_extraction_actually_found_messages(self):
        """A regex that matches nothing would make the test above vacuously pass —
        the same failure mode as a guard filtering on the wrong resource Type."""
        found = self._failure_messages()
        assert len(found) >= 8, f"only found {found!r}; the extraction regex has probably drifted"

    def test_every_vocabulary_entry_is_still_reachable(self):
        """The other direction: a needle nobody emits any more is dead weight that
        makes the ordering harder to reason about than it needs to be."""
        src = _SRC.read_text()
        assert [needle for needle, _ in _CLEANUP_FAILURE_KINDS if needle not in src] == []
