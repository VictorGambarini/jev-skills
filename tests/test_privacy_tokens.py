"""An unlabelled random key is sensitive; long identifiers, paths and digests are not.

The positive cases are generated from a fixed seed, so no key-shaped literal lives in the repo
(scripts/check_release.py would rightly flag one) and every run tests the same strings.
"""
import random
import string
import unittest

from jevkit import compact, privacy

URLSAFE = string.ascii_letters + string.digits + "-_"


def tokens(length, count=200, seed=7):
    rng = random.Random(seed)
    return ["".join(rng.choice(URLSAFE) for _ in range(length)) for _ in range(count)]


class UnlabelledKeys(unittest.TestCase):
    def test_a_pasted_key_with_no_label_is_sensitive(self):
        # The case that was missed: a self-hosted gateway's bearer pasted into a chat turn.
        caught = sum(privacy.is_sensitive(f"got this page with this key: {t}") for t in tokens(43, count=1000))
        self.assertGreaterEqual(caught / 1000, 0.98)

    def test_almost_every_32_char_token_is_caught(self):
        caught = sum(privacy.is_sensitive(token) for token in tokens(32, count=1000))
        self.assertGreaterEqual(caught / 1000, 0.9)  # the rest is still masked by redact()

    def test_readable_names_paths_and_digests_are_not_sensitive(self):
        for text in (
            "VictorGambarini/jev-skills/pull/1",
            "/Users/Victor/Projects/app2/src/components/Header3Layout",
            "getUserProfileByIdentifierAsync2",
            "Cloudflare_clef_flash_Instruct_2026_v2_final",
            "README_SECTION_ChoosingADecisionBackend_v2",
            "AbstractSingletonProxyFactoryBean2024",
            "HTTPServerRequestHandlerV2ForIPv6Clients",
            "ParseJSON2XMLConverterUTF8Base64Decoder",
            "TestCase_GPT4o_vs_Claude35_Sonnet_Eval_Run12",
            "MyApp_Build_2026_10_07_Release_Candidate_RC3",
            "test_every_description_survives_the_picker_whole",
            "c72ed0d4b1e9f3a2c5d6e7f8091a2b3c4d5e6f70",          # a commit sha
            "bc480a46-6901-4f34-a9e9-7f5361abb9da",              # a uuid
        ):
            with self.subTest(text=text):
                self.assertFalse(privacy.is_sensitive(text))

    def test_redact_still_masks_what_it_masked(self):
        token = tokens(43, count=1)[0]
        self.assertNotIn(token, privacy.redact(f"key {token} here"))

    def test_compaction_never_sends_a_turn_holding_one(self):
        token = tokens(43, count=1)[0]
        sent = []

        def transport(body, headers, timeout):
            sent.append(body.decode())
            raise compact.client.JevError("network")

        messages = [{"role": "user", "content": f"here is the key {token}"}] + \
                   [{"role": "assistant", "content": f"ordinary turn number {i} " * 5} for i in range(10)]
        compact.select(messages, transport=transport)
        self.assertFalse(any(token in body for body in sent))
        self.assertFalse(any(token[:12] in body for body in sent))


if __name__ == "__main__":
    unittest.main()
