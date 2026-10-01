"""The requested public star chart remains the README footer."""
from pathlib import Path
import unittest


class StarHistoryTests(unittest.TestCase):
    def test_footer_chart_targets_this_public_repo(self):
        text = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
        footer = text.rsplit("## Star history\n", 1)[1].strip()
        self.assertEqual(footer,
            "[![GitHub star history for Hermes Jev Skills]"
            "(https://api.star-history.com/svg?repos=kerpopule/hermes-jev-skills&type=Date)]"
            "(https://www.star-history.com/#kerpopule/hermes-jev-skills&Date)")


if __name__ == "__main__":
    unittest.main()
