"""Validate canonical skill JSON and build deterministic backend/Pages indexes."""
import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = {"id", "name", "category", "difficulty", "icon", "color", "description",
            "starter", "prompt", "tags"}
ICONS = {"pen", "code", "clock", "layout", "mail", "chart", "book", "spark"}
COLORS = {"peach", "lavender", "mint", "sand", "blue", "rose"}
DIFFICULTIES = {"مبتدئ", "متوسط", "متقدم"}


def validate(skill, seen):
    if not isinstance(skill, dict) or set(skill) != REQUIRED:
        raise ValueError("A skill must contain exactly the documented fields")
    for key in REQUIRED - {"tags"}:
        if not isinstance(skill[key], str) or not skill[key].strip() or len(skill[key]) > 6000:
            raise ValueError(f"Invalid string: {key}")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", skill["id"]):
        raise ValueError("Skill ID must be a safe filename")
    if skill["id"] in seen:
        raise ValueError(f"Duplicate skill ID: {skill['id']}")
    if skill["icon"] not in ICONS or skill["color"] not in COLORS:
        raise ValueError("Unknown presentation token")
    if skill["difficulty"] not in DIFFICULTIES:
        raise ValueError("Unknown difficulty")
    if not isinstance(skill["tags"], list) or not 1 <= len(skill["tags"]) <= 12:
        raise ValueError("Provide 1–12 tags")
    if any(not isinstance(tag, str) or not tag.strip() or len(tag) > 100 for tag in skill["tags"]):
        raise ValueError("Invalid tag")
    seen.add(skill["id"])


def load_skills():
    paths = sorted((ROOT / "skills").glob("*/skill.json"))
    if not paths:
        raise ValueError("No skill JSON found")
    if len(paths) > 1000:
        raise ValueError("Catalog limit: 1000 skills")
    seen, skills = set(), []
    for path in paths:
        if path.stat().st_size > 40000:
            raise ValueError("Skill JSON too large")
        skill = json.loads(path.read_text(encoding="utf-8"))
        validate(skill, seen)
        if path.parent.name != skill["id"]:
            raise ValueError("Directory must match the skill ID")
        skills.append(skill)
    return skills


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def outputs(skills):
    result = {
        ROOT / "backend/skills.json": json_bytes(skills),
        ROOT / "docs/data/index.json": json_bytes({"format": "waha.catalog.v1", "sample": True,
                                                  "skills": skills}),
    }
    for skill in skills:
        result[ROOT / "docs/data/skills" / f"{skill['id']}.json"] = json_bytes(
            {"format": "waha.skill.v1", "sample": True, "skill": skill})
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Validate generated files without writing")
    args = parser.parse_args()
    skills = load_skills()
    expected = outputs(skills)
    for path, content in expected.items():
        if args.check:
            if not path.exists() or path.read_bytes() != content:
                raise SystemExit(f"Outdated generated file: {path.relative_to(ROOT)}; run build_catalog.py")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
    actual_downloads = set((ROOT / "docs/data/skills").glob("*.json"))
    stale = actual_downloads - set(expected)
    if args.check and stale:
        raise SystemExit("Stale skill files: " + ", ".join(p.name for p in stale))
    if not args.check:
        for path in stale:
            path.unlink()
    print(f"Validated {len(skills)} skills; generated indexes {'checked' if args.check else 'updated'}.")


if __name__ == "__main__":
    main()