import copy
import unittest
from unittest import mock

import verify_claim_durability as verifier


class ClaimDurabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.overlay = verifier.read_json(verifier.OVERLAY)

    def setUp(self):
        self.git_blob_patcher = mock.patch.object(
            verifier, "git_blob", return_value=None
        )
        self.git_blob_patcher.start()

    def tearDown(self):
        self.git_blob_patcher.stop()

    def test_current_overlay_verifies(self):
        self.assertEqual(verifier.verify(self.overlay), [])

    def test_source_ledger_tamper_is_rejected(self):
        tampered = copy.deepcopy(self.overlay)
        tampered["source_ledger"]["sha256"] = "0" * 64
        self.assertIn("source ledger hash mismatch", verifier.verify(tampered))

    def test_schema_tamper_is_rejected(self):
        tampered = copy.deepcopy(self.overlay)
        tampered["schema_binding"]["sha256"] = "0" * 64
        self.assertIn("bound schema hash mismatch", verifier.verify(tampered))

    def test_source_claim_record_tamper_is_rejected(self):
        tampered = copy.deepcopy(self.overlay)
        tampered["source_claim_record"]["sha256"] = "0" * 64
        self.assertIn(
            "source claim archive member hash mismatch",
            verifier.verify(tampered),
        )

    def test_present_but_wrong_historical_git_object_is_rejected(self):
        with mock.patch.object(verifier, "git_blob", return_value=b"wrong bytes"):
            self.assertIn(
                "source claim Git object hash mismatch",
                verifier.verify(self.overlay),
            )

    def test_historical_claims_one_byte_tamper_fails(self):
        tampered = copy.deepcopy(self.overlay)
        tampered["historical_claims_source"]["sha256"] = "0" * 64
        self.assertIn("historical claims hash mismatch", verifier.verify(tampered))

    def test_overlay_must_cover_every_source_claim(self):
        tampered = copy.deepcopy(self.overlay)
        tampered["claims"].pop()
        self.assertIn(
            "overlay does not cover the exact source-ledger claim set",
            verifier.verify(tampered),
        )

    def test_mutable_source_cannot_be_upgraded_to_durable(self):
        tampered = copy.deepcopy(self.overlay)
        claim = tampered["claims"][0]
        claim["durability_state"] = "DURABLE"
        errors = verifier.verify(tampered)
        self.assertTrue(
            any("unavailable evidence was upgraded to durable" in error for error in errors)
        )

    def test_universal_novelty_cannot_be_enabled(self):
        tampered = copy.deepcopy(self.overlay)
        novelty = next(
            row
            for row in tampered["claims"]
            if row["claim_id"] == "whole-hybrid-pipeline-whitespace"
        )
        novelty["search_record"]["universal_claim_allowed"] = True
        self.assertIn(
            "universal novelty claim must remain prohibited",
            verifier.verify(tampered),
        )

    def test_unknown_search_queries_cannot_be_silently_filled(self):
        tampered = copy.deepcopy(self.overlay)
        novelty = next(
            row
            for row in tampered["claims"]
            if row["claim_id"] == "whole-hybrid-pipeline-whitespace"
        )
        novelty["search_record"]["exact_queries"] = {
            "status": "COMPLETE",
            "queries": ["invented query"],
        }
        self.assertIn(
            "historical search exact_queries must remain explained UNKNOWN",
            verifier.verify(tampered),
        )

    def test_future_protocol_cannot_drop_negative_evidence(self):
        tampered = copy.deepcopy(self.overlay)
        tampered["future_search_protocol"]["required_capture"].remove(
            "negative_and_disconfirming_results"
        )
        self.assertIn(
            "future search protocol capture set is incomplete",
            verifier.verify(tampered),
        )

    def test_readme_competitor_claim_without_binding_fails(self):
        text = "No existing tool supports this workflow."
        self.assertTrue(verifier.high_risk_claims(text))

    def test_new_novelty_phrase_without_binding_fails(self):
        text = "This is the first-of-its-kind retrieval system."
        self.assertTrue(verifier.high_risk_claims(text))

    def test_common_novelty_bypass_phrases_are_detected(self):
        bypasses = (
            "This is the only local tool for the job.",
            "This product is unique among local systems.",
            "This is the first local search product.",
            "This is unlike any existing product.",
            "There is no comparable system.",
        )
        for text in bypasses:
            with self.subTest(text=text):
                self.assertTrue(verifier.high_risk_claims(text))

    def test_all_source_note_novelty_claims_are_dispositioned(self):
        surface_ids = {
            row["surface_id"]
            for row in self.overlay["claim_surfaces"]
            if row["source_kind"] == "ARCHIVE_EXCERPT"
        }
        self.assertEqual(
            surface_ids,
            {
                "historical-whole-pipeline-nobody-built",
                "historical-tie-animation-novel",
            },
        )
        self.assertEqual(verifier.verify(self.overlay), [])

    def test_supported_empirical_claim_with_unknown_receipt_fails(self):
        tampered = copy.deepcopy(self.overlay)
        disposition = next(
            row
            for row in tampered["historical_claim_dispositions"]
            if row["claim"].startswith("T1 packed")
        )
        disposition["durability_state"] = "SUPPORTED_DURABLE"
        errors = verifier.verify(tampered)
        self.assertTrue(
            any("supported empirical claim lacks reproducibility evidence" in e for e in errors)
        )

    def test_source_excerpts_match_bound_historical_bytes(self):
        self.assertEqual(verifier.verify(self.overlay), [])

    def test_verifier_passes_without_historical_git_object(self):
        self.assertEqual(verifier.verify(self.overlay), [])

    def test_schema_rejects_unknown_properties(self):
        tampered = copy.deepcopy(self.overlay)
        tampered["unexpected"] = "not declared"
        self.assertIn(
            "schema validation: $: unknown property unexpected",
            verifier.verify(tampered),
        )

    def test_competitor_assertions_cannot_be_invented(self):
        tampered = copy.deepcopy(self.overlay)
        novelty = next(
            row
            for row in tampered["claims"]
            if row["claim_id"] == "whole-hybrid-pipeline-whitespace"
        )
        novelty["search_record"]["preserved_candidate_assertions"].append(
            {
                "candidate": "Invented competitor",
                "evidence_state": "NAMED_IN_SOURCE_NOTE_ONLY",
            }
        )
        self.assertIn(
            "historical search candidate assertions changed",
            verifier.verify(tampered),
        )

    def test_allowed_wording_cannot_append_a_universal_claim(self):
        tampered = copy.deepcopy(self.overlay)
        novelty = next(
            row
            for row in tampered["claims"]
            if row["claim_id"] == "whole-hybrid-pipeline-whitespace"
        )
        novelty["search_record"]["allowed_wording"] += " Nobody has built it."
        self.assertIn(
            "allowed novelty wording is not fail-closed",
            verifier.verify(tampered),
        )


if __name__ == "__main__":
    unittest.main()
