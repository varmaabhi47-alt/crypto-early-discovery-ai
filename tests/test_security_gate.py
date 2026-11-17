import unittest
from unittest.mock import AsyncMock, patch

import app


class DiscoverySecurityGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_security_groups_override_opportunity_score(self):
        tokens = [
            {"mint": "eligible", "opportunity_score": 99},
            {"mint": "unknown", "opportunity_score": 100},
            {"mint": "critical_low", "opportunity_score": 10},
            {"mint": "critical_high", "opportunity_score": 1000},
        ]

        async def fake_assessment(_client, token):
            mint = token["mint"]
            if mint == "unknown":
                return {
                    "status": "unavailable",
                    "classification": "unknown",
                    "critical_risk_count": None,
                    "shortlist_action": "hold_security_unknown",
                }
            if mint == "critical_low" or mint == "critical_high":
                return {
                    "status": "assessed",
                    "classification": "critical_review_required",
                    "critical_risk_count": 1,
                    "shortlist_action": "exclude_from_positive_shortlist_pending_manual_review",
                }
            return {
                "status": "assessed",
                "classification": "no_reported_risks_not_proof_of_safety",
                "critical_risk_count": 0,
                "shortlist_action": "manual_review_required",
            }

        # The first ten candidates are assessed by design. To test the unassessed
        # path, provide eleven candidates and place this one outside the assessed set.
        tokens.extend(
            {"mint": f"filler_{i}", "opportunity_score": 50 - i}
            for i in range(6)
        )
        tokens.append({"mint": "unassessed", "opportunity_score": -100})

        with patch.object(app, "assess_discovery_security", new=AsyncMock(side_effect=fake_assessment)):
            result = await app.apply_discovery_security_gate(object(), tokens)

        names = [item["mint"] for item in result]
        eligible_index = names.index("eligible")
        unknown_index = names.index("unknown")
        unassessed_index = names.index("unassessed")
        critical_high_index = names.index("critical_high")
        critical_low_index = names.index("critical_low")

        self.assertLess(eligible_index, unknown_index)
        self.assertLess(eligible_index, unassessed_index)
        self.assertLess(eligible_index, critical_high_index)
        self.assertLess(unknown_index, critical_high_index)
        self.assertLess(unassessed_index, critical_high_index)
        self.assertLess(critical_high_index, critical_low_index)

        by_mint = {item["mint"]: item for item in result}
        self.assertTrue(by_mint["eligible"]["shortlist_eligible"])
        self.assertFalse(by_mint["unknown"]["shortlist_eligible"])
        self.assertEqual(by_mint["unknown"]["shortlist_action"], "hold_security_unknown")
        self.assertFalse(by_mint["unassessed"]["shortlist_eligible"])
        self.assertEqual(by_mint["unassessed"]["shortlist_action"], "hold_pending_security_check")
        self.assertFalse(by_mint["critical_high"]["shortlist_eligible"])
        self.assertEqual(by_mint["critical_high"]["shortlist_action"],
                         "exclude_from_positive_shortlist_pending_manual_review")


    async def test_missing_mint_and_assessment_exception_are_held_back(self):
        tokens = [
            {"mint": "healthy", "opportunity_score": 20},
            {"mint": "provider_error", "opportunity_score": 1000},
            {"opportunity_score": 500},
        ]

        async def fake_assessment(_client, token):
            if token["mint"] == "provider_error":
                raise RuntimeError("simulated provider failure")
            return {
                "status": "assessed",
                "classification": "no_reported_risks_not_proof_of_safety",
                "critical_risk_count": 0,
                "shortlist_action": "manual_review_required",
            }

        with patch.object(app, "assess_discovery_security", new=AsyncMock(side_effect=fake_assessment)):
            result = await app.apply_discovery_security_gate(object(), tokens)

        by_mint = {item.get("mint"): item for item in result}
        self.assertTrue(by_mint["healthy"]["shortlist_eligible"])
        self.assertFalse(by_mint["provider_error"]["shortlist_eligible"])
        self.assertEqual(by_mint["provider_error"]["security_risk_classification"], "unknown")
        self.assertEqual(by_mint["provider_error"]["shortlist_action"], "hold_security_unknown")
        missing_mint = next(item for item in result if "mint" not in item)
        self.assertFalse(missing_mint["shortlist_eligible"])
        self.assertEqual(missing_mint["security_risk_classification"], "unknown")
        self.assertEqual(missing_mint["shortlist_action"], "hold_security_unknown")

    async def test_only_top_ten_candidates_are_assessed(self):
        tokens = [
            {"mint": f"candidate_{i}", "opportunity_score": 100 - i}
            for i in range(12)
        ]
        async def fake_assessment(_client, token):
            return {
                "status": "assessed",
                "classification": "no_reported_risks_not_proof_of_safety",
                "critical_risk_count": 0,
                "shortlist_action": "manual_review_required",
            }

        assessor = AsyncMock(side_effect=fake_assessment)
        with patch.object(app, "assess_discovery_security", new=assessor):
            result = await app.apply_discovery_security_gate(object(), tokens)

        self.assertEqual(assessor.await_count, 10)
        by_mint = {item["mint"]: item for item in result}
        self.assertTrue(by_mint["candidate_9"]["shortlist_eligible"])
        self.assertFalse(by_mint["candidate_10"]["shortlist_eligible"])
        self.assertFalse(by_mint["candidate_11"]["shortlist_eligible"])
        self.assertEqual(by_mint["candidate_10"]["shortlist_action"], "hold_pending_security_check")
        self.assertEqual(by_mint["candidate_11"]["shortlist_action"], "hold_pending_security_check")


if __name__ == "__main__":
    unittest.main()
