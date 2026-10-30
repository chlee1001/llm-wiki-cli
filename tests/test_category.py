from __future__ import annotations

import io
import json
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from llm_wiki import cli
from llm_wiki.config import WikiError, add_project, load_wiki_at, set_category
from llm_wiki.init import init_central, init_wiki, register_project
from llm_wiki.records import promote
from llm_wiki.search import search


class CategoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clone = Path(self.temp.name).resolve() / "company"
        self.clone.mkdir()
        init_central(self.clone, "company")
        for name in ("api", "web", "ops"):
            register_project(load_wiki_at(self.clone), name, None)
        self.wiki = load_wiki_at(self.clone)

    def config(self) -> dict:
        return tomllib.loads((self.clone / "wiki.toml").read_text(encoding="utf-8"))

    def run_cli(self, *argv: str) -> tuple[int, dict]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(list(argv))
        return code, json.loads(out.getvalue())

    def test_new_projects_start_with_an_empty_category(self) -> None:
        self.assertEqual(self.wiki.categories, {"api": "", "web": "", "ops": ""})
        self.assertEqual(self.config()["projects"]["api"]["category"], "")

    def test_old_wiki_toml_without_category_still_loads(self) -> None:
        (self.clone / "wiki.toml").write_text('[wiki]\nversion = 1\nmode = "umbrella"\nname = "company"\n\n[projects."api"]\npath = "api"\n', encoding="utf-8")
        wiki = load_wiki_at(self.clone)
        self.assertEqual(wiki.categories, {"api": ""})
        self.assertEqual(wiki.projects, {"api": "api"})

    def test_set_category_writes_only_that_project_and_survives_add_project(self) -> None:
        wiki = set_category(self.wiki, "api", "backend")
        self.assertEqual(wiki.categories["api"], "backend")
        wiki = add_project(load_wiki_at(self.clone), "data", "data")
        self.assertEqual(wiki.categories, {"api": "backend", "web": "", "ops": "", "data": ""})
        self.assertEqual(load_wiki_at(self.clone).categories["api"], "backend")
        self.assertEqual(self.config()["projects"]["api"]["path"], "api")

    def test_category_values_are_validated(self) -> None:
        for bad in ("a\nb", "x\x00", "z" * 65, 5):
            with self.subTest(bad), self.assertRaises(WikiError):
                set_category(self.wiki, "api", bad)  # type: ignore[arg-type]
        with self.assertRaisesRegex(WikiError, "Unknown project"):
            set_category(self.wiki, "ghost", "x")
        self.assertEqual(load_wiki_at(self.clone).categories["api"], "")

    def test_non_string_category_in_wiki_toml_is_an_error(self) -> None:
        (self.clone / "wiki.toml").write_text('[wiki]\nversion = 1\nmode = "umbrella"\nname = "company"\n\n[projects."api"]\npath = "api"\ncategory = 3\n', encoding="utf-8")
        with self.assertRaisesRegex(WikiError, "category"):
            load_wiki_at(self.clone)

    def test_folders_stay_flat(self) -> None:
        set_category(self.wiki, "api", "backend")
        self.assertTrue((self.clone / "projects" / "api").is_dir())
        self.assertFalse((self.clone / "projects" / "backend").exists())

    def test_search_by_category_covers_exactly_its_projects(self) -> None:
        wiki = set_category(self.wiki, "api", "backend")
        wiki = set_category(wiki, "ops", "backend")
        for name in ("api", "web", "ops"):
            promote(wiki, f"{name} note", "quokka-term", [], name)
        promote(wiki, "shared note", "quokka-term", [], None, shared=True)
        results, _, _ = search(wiki, "quokka-term", self.clone, None, False, "text", 10, category="backend")
        projects = sorted(result["path"].split("/")[2] for result in results)
        self.assertEqual(projects, ["api", "ops"])

    def test_unknown_category_search_is_an_error(self) -> None:
        with self.assertRaisesRegex(WikiError, "category"):
            search(self.wiki, "x", self.clone, None, False, "text", 10, category="nope")

    def test_cli_category_set_show_search_and_status(self) -> None:
        code, data = self.run_cli("category", "api", "backend", "--root", str(self.clone), "--json")
        self.assertEqual((code, data), (0, {"project": "api", "category": "backend"}))
        code, data = self.run_cli("category", "api", "--root", str(self.clone), "--json")
        self.assertEqual(data["category"], "backend")
        code, data = self.run_cli("add-project", "data", "--category", "analytics", "--root", str(self.clone), "--json")
        self.assertEqual(code, 0)
        self.assertEqual(load_wiki_at(self.clone).categories["data"], "analytics")
        code, data = self.run_cli("status", "--root", str(self.clone), "--json")
        self.assertEqual({item["name"]: item["category"] for item in data["projects"]}, {"api": "backend", "data": "analytics", "ops": "", "web": ""})
        promote(load_wiki_at(self.clone), "n", "walrus-term", [], "api")
        code, data = self.run_cli("search", "walrus-term", "--category", "backend", "--engine", "text", "--root", str(self.clone), "--json")
        self.assertEqual(code, 0)
        self.assertEqual(len(data["results"]), 1)

    def test_cli_category_conflicts_with_project_scope(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["search", "x", "--category", "a", "--project", "api", "--root", str(self.clone), "--json"])
        self.assertEqual(code, 2)

    def test_single_mode_wiki_still_writes_and_reads_category(self) -> None:
        root = Path(self.temp.name).resolve() / "single"
        root.mkdir()
        init_wiki(root, "single", "demo", False, False)
        from llm_wiki.config import discover

        wiki = discover(root)
        self.assertEqual(wiki.categories, {"demo": ""})
        self.assertEqual(set_category(wiki, "demo", "tools").categories["demo"], "tools")


if __name__ == "__main__":
    unittest.main()
