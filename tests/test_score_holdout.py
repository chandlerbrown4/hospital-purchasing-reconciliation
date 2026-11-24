import unittest

from evaluation.score_holdout import score


class HoldoutScoringTests(unittest.TestCase):
    def test_review_distinguishes_unknown_exposure_from_zero(self):
        expected = [{"event_id": "I", "decision": "review", "price_delta_cents": "0"}]
        actual = [{"event_id": "I", "engine_decision": "uncertain_receipts", "active": 1,
                   "price_delta_cents": 0, "unbacked_eaches": 0, "quantity_exposure_cents": 0}]
        self.assertEqual(score(expected, actual)[0]["result"], "wrong amounts")

    def test_preserves_missing_unexpected_and_inactive_invoice_decisions(self):
        expected = [{"event_id": "absent", "decision": "clean"},
                    {"event_id": "dropped", "decision": "review"}]
        actual = [{"event_id": "extra", "engine_decision": "matched", "active": 1},
                  {"event_id": "dropped", "engine_decision": "replay", "active": None}]
        results = {row["event_id"]: row["result"] for row in score(expected, actual)}
        self.assertEqual(results, {"absent": "missing decision", "dropped": "missing decision",
                                   "extra": "unexpected output"})

    def test_counts_false_clean_for_discrepancy_and_review(self):
        expected = [{"event_id": key, "decision": key} for key in ("exception", "review")]
        actual = [{"event_id": key, "engine_decision": "matched", "active": 1}
                  for key in ("exception", "review")]
        self.assertEqual([row["result"] for row in score(expected, actual)],
                         ["false clean", "false clean"])

    def test_rejects_duplicate_event_ids_instead_of_overwriting_an_observation(self):
        row = {"event_id": "I", "engine_decision": "matched", "active": 1}
        with self.assertRaisesRegex(ValueError, "unique invoice event ID"):
            score([], [row, row])
