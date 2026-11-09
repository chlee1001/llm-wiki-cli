"""Validated work history and derived cross-week indexes."""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date, datetime
from pathlib import Path

from .config import Wiki, WikiError, require_writable
from .redact import redact
from .records import TMP_DIR, replace_atomic
from .sync import require_no_rebase

_SLUG = re.compile(r"[a-z0-9가-힣]+(?:-[a-z0-9가-힣]+)*\Z")
_HASH_FIELDS = ("project", "title", "summary", "why", "why_quote", "work_status", "outcome", "decisions", "sources", "refs")
_REF_FIELDS = ("kind", "host", "repo", "number", "key")


def content_hash(record: dict) -> str:
    sub = {key: record.get(key) for key in _HASH_FIELDS}
    if sub["refs"] is not None:
        sub["refs"] = [{key: ref.get(key) for key in _REF_FIELDS} for ref in sub["refs"]]
    return hashlib.sha256(json.dumps(sub, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _validate(wiki: Wiki, data: dict) -> tuple[str, list[dict]]:
    errors = []

    def obj(value, path, allowed, required=()):
        if not isinstance(value, dict):
            errors.append(f"{path} must be an object")
            return {}
        if set(value) - set(allowed):
            errors.append(f"{path} has an unknown field")
        for key in required:
            if key not in value:
                errors.append(f"{path}.{key} is required")
        return value

    def text(value, path):
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{path} must be a nonempty string")
            return ""
        return value

    def array(value, path):
        if not isinstance(value, list):
            errors.append(f"{path} must be an array")
            return []
        return value

    def day(value, path):
        value = text(value, path)
        try:
            parsed = date.fromisoformat(value)
            if parsed.isoformat() != value:
                raise ValueError
            return parsed
        except ValueError:
            errors.append(f"{path} must be an ISO date")
            return None

    def quote(value, path):
        value = text(value, path)
        if len(value) > 200:
            errors.append(f"{path} must be at most 200 characters")

    def slug(value, path):
        value = text(value, path)
        if value and not _SLUG.fullmatch(value):
            errors.append(f"{path} must be a lowercase kebab slug")
        return value

    obj(data, "input", {"schema", "week", "period", "extraction", "records"}, {"schema", "week", "period", "extraction", "records"})
    if redact(json.dumps(data, ensure_ascii=False))[0] != json.dumps(data, ensure_ascii=False):
        errors.append("input contains a secret")
    if data.get("schema") != "work-history/v2":
        errors.append("schema must be work-history/v2")
    week = text(data.get("week"), "week")
    week_start = None
    match = re.fullmatch(r"(\d{4})-W(\d{2})", week)
    if match:
        try:
            week_start = date.fromisocalendar(int(match[1]), int(match[2]), 1)
        except ValueError:
            pass
    if not week_start:
        errors.append("week must be a valid ISO week")
    period = obj(data.get("period"), "period", {"start", "end"}, {"start", "end"})
    start, end = day(period.get("start"), "period.start"), day(period.get("end"), "period.end")
    if start and end and (start > end or (week_start and (start.isocalendar()[:2] != week_start.isocalendar()[:2] or end.isocalendar()[:2] != week_start.isocalendar()[:2]))):
        errors.append("period dates must be ordered and within week")
    extraction = obj(data.get("extraction"), "extraction", {"bundle_sha256", "skill", "model"}, {"bundle_sha256", "skill", "model"})
    if not re.fullmatch(r"[0-9a-f]{64}", str(extraction.get("bundle_sha256", ""))):
        errors.append("extraction.bundle_sha256 must be a sha256 hex digest")
    text(extraction.get("skill"), "extraction.skill")
    text(extraction.get("model"), "extraction.model")
    ids, carries = set(), set()
    records = array(data.get("records"), "records")
    for i, raw in enumerate(records):
        p = f"records[{i}]"
        r = obj(raw, p, {"id", "project", "date", "title", "summary", "why", "why_quote", "work_status", "carry_key", "outcome", "evidence", "verified_hash", "decisions", "refs", "tags", "sources"}, {"id", "project", "date", "title", "summary", "work_status", "evidence", "sources"})
        rid = slug(r.get("id"), f"{p}.id")
        if rid in ids:
            errors.append(f"{p}.id must be unique")
        ids.add(rid)
        project = text(r.get("project"), f"{p}.project")
        if project not in wiki.projects:
            errors.append(f"{p}.project must be registered")
        for field in ("title", "summary"):
            text(r.get(field), f"{p}.{field}")
        for field in ("why", "outcome"):
            if field in r:
                text(r[field], f"{p}.{field}")
        if ("why" in r) != ("why_quote" in r):
            errors.append(f"{p}.why and why_quote must occur together")
        if "why_quote" in r:
            quote(r["why_quote"], f"{p}.why_quote")
        status = r.get("work_status")
        if status not in ("done", "in-progress", "dropped", "unknown"):
            errors.append(f"{p}.work_status is invalid")
        if "carry_key" in r:
            carry = slug(r["carry_key"], f"{p}.carry_key")
            if (project, carry) in carries:
                errors.append(f"{p}.carry_key must be unique per project and week")
            carries.add((project, carry))
        elif status in ("in-progress", "unknown"):
            errors.append(f"{p}.carry_key is required")
        evidence = r.get("evidence")
        if evidence not in ("inferred", "verified"):
            errors.append(f"{p}.evidence is invalid")
        if evidence == "verified":
            try:
                if r.get("verified_hash") != content_hash(r):
                    errors.append(f"{p}.verified_hash does not match")
            except (TypeError, AttributeError):
                errors.append(f"{p}.verified_hash cannot be calculated")
        elif "verified_hash" in r:
            errors.append(f"{p}.verified_hash requires verified evidence")
        source_keys, dates = set(), []
        sources = array(r.get("sources"), f"{p}.sources")
        if not sources:
            errors.append(f"{p}.sources must not be empty")
        for n, raw_source in enumerate(sources):
            sp = f"{p}.sources[{n}]"
            s = obj(raw_source, sp, {"agent", "session", "wrap", "date"}, {"agent", "date"})
            agent = text(s.get("agent"), f"{sp}.agent")
            sd = day(s.get("date"), f"{sp}.date")
            if sd:
                dates.append(sd)
                if week_start and sd.isocalendar()[:2] != week_start.isocalendar()[:2]:
                    errors.append(f"{sp}.date must fall within week")
            if ("session" in s) == ("wrap" in s):
                errors.append(f"{sp} must contain session or wrap")
            elif "session" in s:
                session = text(s["session"], f"{sp}.session")
                if agent not in ("gjc", "claude", "codex"):
                    errors.append(f"{sp}.agent is invalid")
                source_keys.add(f"{agent}:{session}")
            else:
                wrap = text(s["wrap"], f"{sp}.wrap")
                source_keys.add(f"wrap:{wrap}")
                parts = Path(wrap).parts
                if not re.fullmatch(r"projects/[^/]+/sessions/\d{4}/\d{2}/session-[^/]+\.md", wrap) or len(parts) != 6 or parts[1] not in wiki.projects:
                    errors.append(f"{sp}.wrap has an invalid path")
                else:
                    path = wiki.directory / wrap
                    current = path
                    unsafe = False
                    while current != wiki.directory:
                        unsafe |= current.is_symlink()
                        current = current.parent
                    if unsafe or not path.is_file() or not path.resolve().is_relative_to(wiki.directory.resolve()):
                        errors.append(f"{sp}.wrap must be an existing non-symlink file")
        rd = day(r.get("date"), f"{p}.date")
        if rd and dates and rd != max(dates):
            errors.append(f"{p}.date must equal the latest source date")
        for n, raw_decision in enumerate(array(r.get("decisions", []), f"{p}.decisions")):
            dp = f"{p}.decisions[{n}]"
            d = obj(raw_decision, dp, {"id", "decision", "options", "reason", "reason_quote", "supersedes", "source", "quote"}, {"id", "decision", "options", "supersedes", "source", "quote"})
            slug(d.get("id"), f"{dp}.id")
            text(d.get("decision"), f"{dp}.decision")
            for option in array(d.get("options"), f"{dp}.options"):
                text(option, f"{dp}.options")
            for superseded in array(d.get("supersedes"), f"{dp}.supersedes"):
                slug(superseded, f"{dp}.supersedes")
            if ("reason" in d) != ("reason_quote" in d):
                errors.append(f"{dp}.reason and reason_quote must occur together")
            if "reason" in d:
                text(d["reason"], f"{dp}.reason")
                quote(d["reason_quote"], f"{dp}.reason_quote") if "reason_quote" in d else None
            if not isinstance(d.get("source"), str) or d["source"] not in source_keys:
                errors.append(f"{dp}.source must identify a record source")
            quote(d.get("quote"), f"{dp}.quote")
        for n, raw_ref in enumerate(array(r.get("refs", []), f"{p}.refs")):
            rp = f"{p}.refs[{n}]"
            ref = obj(raw_ref, rp, {"kind", "host", "repo", "number", "key", "state", "checked_at", "reason"}, {"kind", "state"})
            for field in ("host", "repo", "key"):
                if field in ref and ref[field] is not None:
                    value = text(ref[field], f"{rp}.{field}")
                    if ".." in value or any(c.isspace() or ord(c) < 32 for c in value):
                        errors.append(f"{rp}.{field} has invalid characters")
            if ref.get("kind") == "jira":
                text(ref.get("key"), f"{rp}.key")
                if ref.get("state") != "unchecked" or "reason" in ref:
                    errors.append(f"{rp} has invalid jira state")
            elif ref.get("kind") == "pr":
                text(ref.get("repo"), f"{rp}.repo")
                if type(ref.get("number")) is not int or ref["number"] < 1:
                    errors.append(f"{rp}.number must be positive")
                if ref.get("state") == "unchecked":
                    if ref.get("reason") not in ("no_token", "auth", "network", "timeout", "redirect", "host_mismatch", "host_unknown", "repo_not_visible"):
                        errors.append(f"{rp}.reason is invalid")
                elif ref.get("state") in ("open", "merged", "closed"):
                    try:
                        checked = datetime.fromisoformat(ref.get("checked_at", ""))
                        if checked.utcoffset() is None:
                            raise ValueError
                    except (ValueError, TypeError):
                        errors.append(f"{rp}.checked_at must include an offset")
                else:
                    errors.append(f"{rp}.state is invalid")
            else:
                errors.append(f"{rp}.kind is invalid")
        for tag in array(r.get("tags", []), f"{p}.tags"):
            text(tag, f"{p}.tags")
    if errors:
        raise WikiError("; ".join(errors))
    return week, records


_MARKER = "<!-- history-records: "


def _safe(path: Path, root: Path) -> None:
    if not path.resolve().is_relative_to(root.resolve()):
        raise WikiError(f"History path escapes wiki: {path}")
    while path != root:
        if path.is_symlink():
            raise WikiError(f"History path is a symbolic link: {path}")
        path = path.parent


def _read_records(path: Path) -> list[dict]:
    try:
        content = path.read_text(encoding="utf-8")
        if not content.startswith("---\norigin: history-ingest\n"):
            raise ValueError
        lines = [line for line in content.splitlines() if line.startswith(_MARKER) and line.endswith(" -->")]
        if len(lines) != 1:
            raise ValueError
        records = json.loads(lines[0][len(_MARKER):-4])
        if not isinstance(records, list) or not all(
            isinstance(r, dict)
            and all(isinstance(r.get(k), str) for k in ("project", "date", "id", "title", "work_status", "evidence"))
            and isinstance(r.get("decisions", []), list)
            and all(isinstance(d, dict) and all(isinstance(d.get(k), str) for k in ("id", "decision"))
                    and isinstance(d.get("supersedes"), list) and all(isinstance(s, str) for s in d["supersedes"])
                    for d in r.get("decisions", []))
            and isinstance(r.get("refs", []), list)
            and all(isinstance(ref, dict) and isinstance(ref.get("state"), str) and
                    ((ref.get("kind") == "jira" and isinstance(ref.get("key"), str)) or
                     (ref.get("kind") == "pr" and isinstance(ref.get("repo"), str) and type(ref.get("number")) is int))
                    for ref in r.get("refs", []))
            and ("carry_key" not in r or isinstance(r["carry_key"], str))
            for r in records
        ):
            raise ValueError
        return records
    except (ValueError, OSError) as exc:
        raise WikiError(f"History page metadata is malformed: {path}") from exc


def _inline(value: object) -> str:
    return re.sub(r"([\\`*_{}\[\]<>#|])", r"\\\1", " ".join(str(value).split()))


def _page(wiki: Wiki, data: dict, project: str, records: list[dict]) -> str:
    dump = lambda value: json.dumps(value, ensure_ascii=False)
    refs = [ref for record in records for ref in record.get("refs", [])]
    meta = json.dumps(records, ensure_ascii=True, separators=(",", ":")).replace("<", "\\u003c")
    lines = ["---", "origin: history-ingest", "type: history", f'created: {data["period"]["end"]}', f'week: {dump(data["week"])}', f'project: {dump(project)}', f'category: {dump(wiki.category(project))}', f'refs: {dump(refs)}', f'status: {dump("verified" if all(r["evidence"] == "verified" for r in records) else "inferred")}', "---", "", f"{_MARKER}{meta} -->", "", f'# {_inline(project)} — {data["week"]}']
    for r in records:
        lines += ["", f'<a id="{r["id"]}"></a>', f'## {_inline(r["title"])} ({r["date"]})', "", "### 요약", "", _inline(r["summary"]), "", "### 배경과 이유", "", _inline(r.get("why", "없음"))]
        if "why_quote" in r:
            lines.append(f'> {_inline(r["why_quote"])}')
        lines += ["", "### 의사결정", ""]
        for d in r.get("decisions", []):
            lines.append(f'- {_inline(d["id"])}: {_inline(d["decision"])} (대체: {_inline(", ".join(d["supersedes"])) or "없음"})')
            lines.append(f'  - 선택지: {_inline(", ".join(d["options"])) or "없음"}')
            lines.append(f'  - 인용: {_inline(d["quote"])}')
            if "reason" in d:
                lines.append(f'  - 이유: {_inline(d["reason"])} — {_inline(d["reason_quote"])}')
        lines += ["", "### 결과와 작업 상태", "", _inline(r.get("outcome", "기록 없음")), f'- 상태: {r["work_status"]}', "", "### 티켓·PR", ""]
        lines += [f'- {_inline(json.dumps(ref, ensure_ascii=False))}' for ref in r.get("refs", [])]
        lines += ["", "### 근거", ""]
        for source in r["sources"]:
            if "wrap" in source:
                relative = os.path.relpath(wiki.directory / source["wrap"], wiki.directory / "projects" / project / "history" / data["week"][:4])
                lines.append(f'- [wrap]({relative}) ({source["date"]})')
            else:
                lines.append(f'- {_inline(source["agent"] + ":" + source["session"])} ({source["date"]})')
        lines += ["", "### 근거 수준", "", "verified" if r["evidence"] == "verified" else "inferred: 인용은 원문에 있으나 해석은 검토 전"]
    lines += ["", f'추출: {_inline(data["extraction"]["skill"])} · {data["extraction"]["bundle_sha256"][:12]}']
    return "\n".join(lines) + "\n"


def _week(value: str) -> None:
    match = re.fullmatch(r"(\d{4})-W(\d{2})", value)
    try:
        if not match:
            raise ValueError
        date.fromisocalendar(int(match[1]), int(match[2]), 1)
    except (ValueError, TypeError) as exc:
        raise WikiError("week must be a valid ISO week") from exc


def _entries(wiki: Wiki, extra: dict | None = None, before: str | None = None, through: str | None = None, project: str | None = None) -> list[tuple[str, str, str, dict]]:
    root = wiki.directory
    entries = []
    for path in sorted(root.glob("projects/*/history/*/*.md")):
        week = path.stem
        _week(week)
        page_project = path.relative_to(root).parts[1]
        if (before and week >= before) or (through and week > through) or (project is not None and page_project != project):
            continue
        _safe(path, root)
        if extra is not None and week == extra["week"]:
            continue
        for record in _read_records(path):
            if record.get("project") != page_project:
                raise WikiError(f"History page metadata is malformed: {path}")
            entries.append((week, page_project, path.relative_to(root).as_posix(), record))
    if extra is not None:
        week = extra["week"]
        for record in extra["records"]:
            record_project = record["project"]
            if (project is None or record_project == project) and (before is None or week < before) and (through is None or week <= through):
                entries.append((week, record_project, f"projects/{record_project}/history/{week[:4]}/{week}.md", record))
    return sorted(entries, key=lambda e: (e[0], e[3]["date"], e[3]["id"]))


def handoff_threads(wiki: Wiki, project: str | None) -> dict[str, dict]:
    threads: dict[str, dict] = {}
    for _, _, _, record in _entries(wiki, project=project):
        wraps = sorted({source["wrap"] for source in record.get("sources", []) if "wrap" in source}, key=lambda wrap: (Path(wrap).name, wrap))
        for index, wrap in enumerate(wraps):
            by = wraps[index + 1] if index + 1 < len(wraps) else None
            previous = threads.get(wrap)
            if previous is None or previous["by"] is not None:
                threads[wrap] = {"record_id": record["id"], "work_status": record["work_status"], "by": by}
    return threads


def _calculate(entries: list[tuple[str, str, str, dict]], before: str | None, through: str | None) -> dict:
    open_work = {}
    decisions = {}
    weeks = set()
    for week, project, path, record in entries:
        if (before and week >= before) or (through and week > through):
            continue
        weeks.add(week)
        carry = record.get("carry_key")
        if carry:
            key = (project, carry)
            if record["work_status"] in ("done", "dropped"):
                open_work.pop(key, None)
            else:
                since = open_work[key]["since"] if key in open_work else week
                open_work[key] = {"project": project, "carry_key": carry, "record_id": record["id"], "title": record["title"], "work_status": record["work_status"], "evidence": record["evidence"], "since": since, "last_week": week, "path": path}
        for decision in record.get("decisions", []):
            key = (project, decision["id"])
            if key in decisions:
                raise WikiError(f"Duplicate decision id: {decision['id']}")
            decisions[key] = {"project": project, "id": decision["id"], "decision": decision["decision"], "week": week, "status": "active", "superseded_by": None, "path": path, "_supersedes": decision["supersedes"]}
    for key, entry in decisions.items():
        for previous in entry.pop("_supersedes"):
            target = decisions.get((key[0], previous))
            if target is None or target is entry or target["week"] > entry["week"]:
                raise WikiError(f"Invalid supersedes target: {previous}")
            if target["superseded_by"] is not None:
                raise WikiError(f"Decision already superseded: {previous}")
            target["superseded_by"] = entry["id"]
            target["status"] = f'superseded by {entry["id"]} ({entry["week"]})'
    for key, entry in decisions.items():
        seen = set()
        current = entry
        while current["superseded_by"] is not None:
            if current["id"] in seen:
                raise WikiError(f"Decision supersedes cycle: {key[1]}")
            seen.add(current["id"])
            current = decisions[(key[0], current["superseded_by"])]
    return {"open_work": list(open_work.values()), "decisions": list(decisions.values()), "weeks": sorted(weeks), "before": before, "through": through}


def state(wiki: Wiki, project: str | None = None, before: str | None = None, through: str | None = None, extra: dict | None = None) -> dict:
    if project is not None and project not in wiki.projects:
        raise WikiError("Project is not registered")
    for bound in (before, through):
        if bound is not None:
            _week(bound)
    entries = _entries(wiki, extra, before, through, project)
    return _calculate(entries, before, through)


def _indexes(wiki: Wiki, entries: list[tuple[str, str, str, dict]], projects: set[str]) -> dict[Path, str]:
    root = wiki.directory
    result = {}
    calculated = _calculate(entries, None, None)
    for project in sorted(projects):
        lines = ["---", "origin: history-ingest", "type: history-index", "---", "", "# 작업 이력", "", "## 열린 작업", ""]
        for item in calculated["open_work"]:
            if item["project"] == project:
                lines.append(f'- [{_inline(item["title"])}]({item["last_week"][:4]}/{item["last_week"]}.md#{item["record_id"]}) ({item["since"]}–{item["last_week"]})')
        lines += ["", "## 결정 원장", ""]
        for item in calculated["decisions"]:
            if item["project"] == project:
                lines.append(f'- {_inline(item["id"])}: {_inline(item["decision"])} — {item["status"]} ({item["week"]}) [{item["week"]}]({item["week"][:4]}/{item["week"]}.md)')
        lines += ["", "## 연대기", ""]
        for week, path in sorted({(e[0], e[2]) for e in entries if e[1] == project}, reverse=True):
            lines.append(f'- [{week}]({week[:4]}/{week}.md)')
        result[root / "projects" / project / "history/index.md"] = "\n".join(lines) + "\n"
    tickets = {}
    latest = {}
    for week, project, path, record in entries:
        for ref in record.get("refs", []):
            key = ref.get("key") if ref["kind"] == "jira" else f'{ref.get("host") or ""}/{ref["repo"]}#{ref["number"]}'
            tickets.setdefault(key, {})[(project, week, path)] = ref
            latest[key] = ref
    lines = ["---", "origin: history-ingest", "type: history-index", "---", "", "# 티켓·PR", ""]
    for key, links in sorted(tickets.items()):
        lines += [f'## {_inline(key)}', ""]
        ref = latest[key]
        for project, week, path in sorted(links):
            lines.append(f'- [{_inline(project)} {week}](../../{path}) — '+_inline(ref["state"])+" "+_inline(ref.get("checked_at", "")))
        lines.append("")
    result[root / "shared/history/tickets.md"] = "\n".join(lines) + "\n"
    return result


def ingest(wiki: Wiki, data: dict, check: bool = False) -> dict:
    week, records = _validate(wiki, data)
    root = wiki.directory
    existing = list(root.glob(f"projects/*/history/{week[:4]}/{week}.md"))
    grouped = {}
    for record in records:
        grouped.setdefault(record["project"], []).append(record)
    pages = {root / "projects" / p / "history" / week[:4] / f"{week}.md": _page(wiki, data, p, rs) for p, rs in sorted(grouped.items())}
    entries = _entries(wiki, data)
    indexes = _indexes(wiki, entries, set(grouped) | {p.relative_to(root).parts[1] for p in existing})
    for path in set(existing) | set(pages) | set(indexes):
        _safe(path, root)
        if path.exists():
            if path in indexes:
                if not path.read_text(encoding="utf-8").startswith("---\norigin: history-ingest\n"):
                    raise WikiError(f"Refusing to replace a non-history index: {path}")
            else:
                _read_records(path)
    result = {"week": week, "records": len(records), "pages": [path.relative_to(root).as_posix() for path in pages], "indexes": [path.relative_to(root).as_posix() for path in indexes], "check": check, "state": _calculate(entries, None, week)}
    for content in {**pages, **indexes}.values():
        if redact(content)[0] != content:
            raise WikiError("History output contains a secret")
    if check:
        return result
    require_writable(wiki)
    require_no_rebase(wiki)
    for path, content in {**pages, **indexes}.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        replace_atomic(path, content, root / TMP_DIR)
    for path in existing:
        if path not in pages:
            path.unlink()
    return result
