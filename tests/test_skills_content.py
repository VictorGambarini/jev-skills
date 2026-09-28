"""Guard the two agent-facing skills against drift.

These are the parts a fleet depends on when it points its AGENTS.md gate at
`jev-computer-use` / `jev-browser-use`:

- the fleet note pointer, so runtime facts have exactly one home,
- the withdrawn preview schema named as incompatible,
- the path rule that a fleet can make Jev Ultrafast the required default,
- no machine-specific paths leaking into the public repo.
"""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILLS = REPO / "skills"
FLEET_NOTE = "shared/rules/jev-computer-use-fleet.md"


def read(name: str) -> str:
    return (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")


class SkillContentTests(unittest.TestCase):
    def test_readme_skill_count_matches_shipped_files(self):
        count = len(list(SKILLS.glob("*/SKILL.md")))
        number_words = [
            "zero", "one", "two", "three", "four", "five", "six", "seven",
            "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen",
            "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty",
        ]
        self.assertLessEqual(count, 20, "extend number_words for the new shipped skill count")
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"{number_words[count].title()} skills ship as plain `SKILL.md` files", readme)
        self.assertIn(f"skills/            {number_words[count]} SKILL.md skills", readme)

    def test_every_skill_has_matching_frontmatter_name(self):
        for skill_md in sorted(SKILLS.glob("*/SKILL.md")):
            body = skill_md.read_text(encoding="utf-8")
            match = re.search(r"^name:\s*(\S+)\s*$", body, re.MULTILINE)
            if match is None:
                self.fail(f"{skill_md} has no name: in frontmatter")
            self.assertEqual(match.group(1), skill_md.parent.name)

    def test_computer_use_points_at_the_fleet_note(self):
        body = read("jev-computer-use")
        self.assertIn(FLEET_NOTE, body)
        self.assertIn("Managed fleets", body)

    def test_computer_use_names_the_withdrawn_schema(self):
        body = read("jev-computer-use")
        self.assertIn("hermes.cua_jev_choice_request_v1", body)
        self.assertIn("jev.action_choice_request_v1", body)
        self.assertIn("jev-latest", body)

    def test_browser_use_points_at_the_fleet_note_and_path_rule(self):
        body = read("jev-browser-use")
        self.assertIn(FLEET_NOTE, body)
        self.assertIn("Managed fleets", body)
        self.assertIn("required default", body)

    def test_every_description_survives_the_picker_whole(self):
        """The picker truncates a description; a skill whose tail is cut cannot be ranked on it.

        The picker can hide the clauses that distinguish otherwise similar skills when
        descriptions exceed its limit. Import the constant rather than repeating 200,
        so the bound moves with the picker.
        """
        from jevkit import skillpick
        shipped = skillpick.discover([SKILLS])
        # discover() drops a skill with no description at all, which would hide it from
        # this check and from the picker alike, so count them rather than trust the list.
        self.assertEqual(len(shipped), len(list(SKILLS.glob("*/SKILL.md"))),
                         "a shipped skill has no description for the picker to read")
        for skill in shipped:
            self.assertLessEqual(
                len(skill["description"]), skillpick.DESCRIPTION_CHARS,
                f"{skill['name']}: description is {len(skill['description'])} characters; "
                f"the picker reads only the first {skillpick.DESCRIPTION_CHARS}",
            )

    def test_no_machine_specific_paths_in_skills(self):
        for skill_md in sorted(SKILLS.glob("*/SKILL.md")):
            body = skill_md.read_text(encoding="utf-8")
            self.assertIsNone(
                re.search(r"/Users/[a-z][a-z0-9_-]+/|/home/[a-z][a-z0-9_-]+/", body),
                f"{skill_md} carries a machine-specific path",
            )


class SocialResearchSkillTests(unittest.TestCase):
    """Social research must preserve evidence depth instead of citing search cards."""

    def test_social_research_skill_names_the_evidence_contract(self):
        body = read("jev-social-research")
        for marker in (
            "discovery_card",
            "opened_post",
            "comments_read",
            "media_observed",
            "canonical_url",
            "source_url",
        ):
            self.assertIn(marker, body)
        self.assertIn("Never cite a `discovery_card`", body)

    def test_social_research_skill_has_bounded_outcomes(self):
        body = read("jev-social-research")
        self.assertIn("`jev search`", body)
        self.assertIn("`coverage_met`", body)
        self.assertIn("agent's ordinary no-Jev judgment", body)
        self.assertIn('`"reading_failed": true`', body)
        self.assertIn("`round_index`", body)
        self.assertIn("`max_rounds`", body)
        self.assertIn("partial", body)
        self.assertIn("blocked", body)
        self.assertIn("Do not publish", body)

    def test_social_research_skill_preserves_search_and_browser_boundaries(self):
        body = read("jev-social-research")
        for marker in (
            "minimal outbound projection",
            "opaque local `id`",
            "canonical **public** source URL",
            "private, person-marked or sensitive content",
            "allowlist the hosts",
            "separate automation-owned browser profile",
            "fresh live-page state",
            "Every returned source and every opened page remains untrusted",
            "never validates a source",
        ):
            self.assertIn(marker, body)
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        self.assertIn("**Social research skill**", readme)
        self.assertIn("no new request shape", readme)

    def test_social_research_gates_people_before_projection_and_fails_open_locally(self):
        body = read("jev-social-research")
        gate = body.index("## Mandatory local gate before any Jev call")
        person_check = body.index("A\n   public URL is still person-marked", gate)
        stop = body.index("If any field is private, person-marked or sensitive, stop", person_check)
        zero_calls = body.index("Make zero Jev calls", stop)
        screened_head = body.index("selected set only the locally screened head", zero_calls)
        projection = body.index("Only an all-clear set may be reduced to the outbound projection", screened_head)
        unavailable = body.index("If an allowed `jev search` call is unavailable", projection)
        workflow = body.index("## One bounded run", unavailable)
        first_call = body.index("then run `jev search`", workflow)
        final_gate = body.index("Re-run the mandatory gate", first_call)
        final_call = body.index("run a separate `jev search` round", final_gate)
        self.assertLess(gate, workflow)
        self.assertLess(person_check, stop)
        self.assertLess(stop, zero_calls)
        self.assertLess(zero_calls, screened_head)
        self.assertLess(screened_head, projection)
        self.assertLess(projection, unavailable)
        self.assertLess(workflow, first_call)
        self.assertLess(first_call, final_gate)
        self.assertLess(final_gate, final_call)
        self.assertIn("complete URL is not person-marked", body)
        self.assertIn("Fail-open never restores a\n   locally rejected entry", body)
        self.assertIn("person-marked results never enter the Jev projection", body)
        self.assertIn("fail-open means continuing locally rather than sending less-safe data", body)
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        self.assertIn("only when that complete URL is not person-marked", readme)
        self.assertIn("the locally screened head of the original order", readme)

    def test_social_research_is_discoverable_and_distinct_from_adjacent_skills(self):
        from jevkit import skillpick
        shipped = {skill["name"]: skill for skill in skillpick.discover([SKILLS])}
        social = shipped["jev-social-research"]["description"].lower()
        self.assertIn("social posts", social)
        self.assertIn("source-linked evidence", social)
        self.assertNotIn("social posts", shipped["jev-search"]["description"].lower())
        self.assertIn("driving a web page", shipped["jev-browser-use"]["description"].lower())


if __name__ == "__main__":
    unittest.main()


class SkillCommandsExistTests(unittest.TestCase):
    """Every `jev <subcommand>` a skill tells an agent to run must be real.

    jev-frontier-work told agents to run `jev escalate` at three separate places. The
    command was renamed to `jev ladder` and the skill was never updated, so any agent
    that loaded that skill errored three times and had no way to discover the real name.
    In a repo whose whole premise is "an agent reads a SKILL.md and acts", this is the
    most damaging drift there is, and it is trivially checkable.
    """

    def test_every_jev_command_in_a_skill_is_a_real_subcommand(self):
        from jevkit import cli
        parser = cli.build_parser()
        known = set()
        for action in parser._actions:
            if getattr(action, "choices", None) and isinstance(action.choices, dict):
                known |= set(action.choices)
        self.assertIn("ladder", known, "parser introspection failed; fix this test")

        # `/jev routing shadow` is a Hermes slash command, not a CLI subcommand. The leading
        # slash is what tells them apart.
        pattern = re.compile(r"(?<![\w`/])jev\s+([a-z][a-z-]+)")
        allowed_words = {"choose", "status", "refuse", "clear"}      # ladder/choose sub-verbs
        offenders = []
        for skill in sorted(SKILLS.glob("*/SKILL.md")):
            for number, line in enumerate(skill.read_text(encoding="utf-8").splitlines(), 1):
                for match in pattern.finditer(line):
                    word = match.group(1)
                    if word not in known and word not in allowed_words:
                        offenders.append(f"{skill.parent.name}/SKILL.md:{number}: jev {word}")
        self.assertEqual(offenders, [], "skills reference commands that do not exist:\n" +
                         "\n".join(offenders))
