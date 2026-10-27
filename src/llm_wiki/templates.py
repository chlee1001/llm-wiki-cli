"""Markdown templates owned by llm-wiki."""
from __future__ import annotations


def agents_block(wiki_dir: str) -> str:
    return f"""<!-- llm-wiki:begin -->
## LLM wiki

Read `{wiki_dir}/index.md` first. Use `llm-wiki search` scoped to the current project before work, then run `llm-wiki wrap` at session end. Put uncertain facts in `inbox/candidates/` with `llm-wiki promote`.

The wiki ranks below source code, tests, and AGENTS.md. Do not record secrets.
<!-- llm-wiki:end -->"""


GJC_FRONT_MATTER = """---
alwaysApply: false
description: Before work in this repo or umbrella, use llm-wiki search; at session end run llm-wiki wrap.
---
"""


def gjc_rule(wiki_dir: str) -> str:
    return f"""<!-- llm-wiki:begin -->
Read `{wiki_dir}/index.md`, then search the current project with `llm-wiki search`. Run `llm-wiki wrap` at session end. Send uncertain facts to candidates with `llm-wiki promote`. Source code, tests, and AGENTS.md outrank this wiki. Never record secrets.
<!-- llm-wiki:end -->
"""


def wiki_readme(name: str) -> str:
    return f"""# {name} LLM Wiki

Read `index.md` before using this wiki. Search the current project before work and write a session record with `llm-wiki wrap` when work ends. Use candidates for uncertain claims.

Source code, tests, and AGENTS.md outrank this wiki. Do not record secrets.
"""


def index(projects: dict[str, str]) -> str:
    lines = ["# LLM Wiki Index", "", "## Projects", "", "<!-- llm-wiki:projects:begin -->"]
    if projects:
        lines.extend(f"- [{name}](projects/{name}/knowledge/) — `{path}`" for name, path in sorted(projects.items()))
    else:
        lines.append("- No registered projects.")
    lines.extend(("<!-- llm-wiki:projects:end -->", "", "## Shared", "", "- [Shared knowledge](shared/knowledge/)"))
    return "\n".join(lines) + "\n"
