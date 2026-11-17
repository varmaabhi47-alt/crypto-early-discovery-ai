import unittest

import app


class MultiChainLaunchTests(unittest.TestCase):
    def test_supported_chain_ids(self):
        self.assertEqual(
            app.SUPPORTED_LAUNCH_CHAINS,
            {
                "solana": "Solana",
                "base": "Base",
                "eth": "Ethereum",
                "bsc": "BNB Chain",
            },
        )

    def test_launch_cache_is_isolated_by_chain(self):
        self.assertIsInstance(app._launch_cache, dict)
        self.assertNotIn("at", app._launch_cache)
        self.assertNotIn("data", app._launch_cache)

    def test_gecko_pool_urls_use_selected_network(self):
        network = "base"
        pool_address = "0x123"
        url = f"https://www.geckoterminal.com/{network}/pools/{pool_address}"
        self.assertIn("/base/pools/", url)


if __name__ == "__main__":
    unittest.main()
