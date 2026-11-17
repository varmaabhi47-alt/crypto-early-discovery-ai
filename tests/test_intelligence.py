import unittest

from intelligence import aggregate_wallets, classify_sentiment


class IntelligenceHelperTests(unittest.TestCase):
    def test_positive_sentiment(self):
        self.assertEqual(classify_sentiment("Bullish breakout, strong growth"), "positive")

    def test_negative_sentiment(self):
        self.assertEqual(classify_sentiment("Possible rug scam, avoid and sell"), "negative")

    def test_mixed_sentiment_is_neutral(self):
        self.assertEqual(classify_sentiment("Bullish but possible scam"), "neutral")

    def test_wallet_aggregation_uses_owner_not_token_account(self):
        creator = "11111111111111111111111111111111"
        owner = "So11111111111111111111111111111111111111112"
        token_account = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
        rows = [
            {"owner": owner, "address": token_account, "pct": 4.0, "insider": True},
            {"owner": owner, "address": token_account, "pct": 2.0, "insider": False},
            {"address": token_account, "pct": 90.0}
        ]
        result = aggregate_wallets(creator, rows)
        by_address = {item["address"]: item for item in result}
        self.assertIn(creator, by_address)
        self.assertIn(owner, by_address)
        self.assertNotIn(token_account, by_address)
        self.assertEqual(by_address[owner]["holder_pct"], 6.0)
        self.assertTrue(by_address[owner]["insider_flagged"])


if __name__ == "__main__":
    unittest.main()
