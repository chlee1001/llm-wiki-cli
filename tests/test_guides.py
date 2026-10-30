"""Generated agent guides and the README must describe the commands and statuses the CLI really has."""
from __future__ import annotations

import argparse
import re
import shlex
import unittest
from pathlib import Path

from llm_wiki import cli, sync
from llm_wiki.curate import DECIDED, PENDING
from llm_wiki.templates import agents_block, gjc_rule, gjc_skill, wiki_readme

README = Path(__file__).resolve().parents[1] / "README.md"


def commands_in(text: str) -> list[str]:
    """Every ``llm-wiki ...`` line inside fenced sh blocks, without heredoc redirections or comments."""
    found = []
    for block in re.findall(r"```sh\n(.*?)```", text, re.S):
        for line in block.splitlines():
            line = line.strip()
            if line.startswith("llm-wiki "):
                found.append(line.split("<<", 1)[0].strip())
    return found


def subcommands() -> set[str]:
    actions = [action for action in cli.parser()._actions if isinstance(action, argparse._SubParsersAction)]
    return set(actions[0].choices)


def guard_reasons() -> set[str]:
    """Every ``reason`` a sync ``guard`` error can carry, read from the source that raises it."""
    source = Path(sync.__file__).read_text(encoding="utf-8")
    return set(re.findall(r'SyncError\("guard", [^\n]*reason="([a-z_]+)"', source))


class GuideTests(unittest.TestCase):
    guides = (agents_block("llm-wiki"), gjc_rule("llm-wiki"))
    # Guidance written into a workspace that roots.json maps to a central clone.
    mapped = (agents_block(None), gjc_rule(None), gjc_skill(None))

    def test_mapped_guides_name_no_local_wiki_and_keep_every_step(self) -> None:
        local = (*self.guides, gjc_skill("llm-wiki"))
        for guide, original in zip(self.mapped, local):
            with self.subTest(guide=guide[:60]):
                self.assertNotIn("`llm-wiki/", guide)
                self.assertIn("roots.json", guide)
                self.assertEqual(commands_in(guide), commands_in(original))

    def test_guides_say_setup_is_the_users_job(self) -> None:
        for guide in (*self.guides, *self.mapped[:2]):
            with self.subTest(guide=guide[:60]):
                self.assertIn("No llm-wiki found", guide)
                self.assertIn("roots.json", guide)
                self.assertIn("llm-wiki init --path", guide)
                self.assertIn("do not run `init`", guide)

    def test_session_start_pulls_then_briefs(self) -> None:
        for guide in self.guides:
            pull, start = guide.find("llm-wiki sync --pull-only --json"), guide.find("llm-wiki brief")
            self.assertNotEqual(pull, -1)
            self.assertGreater(start, pull)
            self.assertIn("`guard`", guide)

    def test_session_end_wraps_then_syncs(self) -> None:
        for guide in (*self.guides, gjc_skill("llm-wiki")):
            wrap = guide.find("llm-wiki wrap --input-json - --json")
            self.assertGreater(guide.find("llm-wiki sync --json", wrap), wrap)
            for code in ("`conflict`", "`secret_detected`"):
                self.assertIn(code, guide)

    def test_guides_cover_take_and_owner_only_decisions(self) -> None:
        for guide in self.guides:
            self.assertIn("llm-wiki take", guide)
            self.assertIn("--decided-by owner", guide)
            self.assertIn("only the owner", guide)
            self.assertIn("llm-wiki review", guide)

    def test_every_documented_command_parses(self) -> None:
        texts = {"agents": self.guides[0], "rule": self.guides[1], "skill": gjc_skill("llm-wiki"), "readme": README.read_text(encoding="utf-8")}
        for name, text in texts.items():
            commands = commands_in(text)
            self.assertTrue(commands, name)
            for command in commands:
                with self.subTest(name=name, command=command):
                    argv = shlex.split(command, comments=True)[1:]
                    cli.parser().parse_args(argv)

    def test_every_guard_reason_is_explained(self) -> None:
        reasons = guard_reasons()
        self.assertEqual(reasons, {"toplevel", "no_remote", "rebase_in_progress", "missing_directory", "no_git"})
        for name, text in {"agents": self.guides[0], "rule": self.guides[1], "skill": gjc_skill("llm-wiki"), "readme": README.read_text(encoding="utf-8")}.items():
            for reason in reasons:
                with self.subTest(name=name, reason=reason):
                    self.assertIn(f"`{reason}`", text)

    def test_wiki_readme_mentions_brief_and_sync(self) -> None:
        text = wiki_readme("company")
        self.assertIn("llm-wiki brief", text)
        self.assertIn("llm-wiki sync", text)


class ReadmeTests(unittest.TestCase):
    text = README.read_text(encoding="utf-8")

    def test_command_table_lists_every_subcommand(self) -> None:
        section = self.text.split("\n## 명령\n", 1)[1].split("\n## ", 1)[0]
        listed = set(re.findall(r"^\| `([a-z-]+)[^`]*` \|", section, re.M))
        self.assertEqual(listed, subcommands())

    def test_status_table_matches_the_curation_states(self) -> None:
        section = self.text.split("## 후보 상태", 1)[1].split("\n## ", 1)[0]
        rows = set(re.findall(r"^\| `([a-z-]+)` \|", section, re.M))
        self.assertEqual(rows, set(PENDING) | set(DECIDED))

    def test_owner_steps_are_documented_not_automated(self) -> None:
        for phrase in ("deploy key", "roots.json", "init --central", "sync --pull-only", "merge=union"):
            self.assertIn(phrase, self.text)


if __name__ == "__main__":
    unittest.main()
