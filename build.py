#!/usr/bin/env python3
"""Assemble, validate and install the two history-analysis skills.

Source layout (edit these):
  common/            shared by both skills: scripts/ and references/
  claude/SKILL.md    Claude Code specific instructions
  copilot/SKILL.md   GitHub Copilot specific instructions

Each installed skill must be self-contained, so the build copies `common/` into every skill:
  dist/claude-session-analyzer/   = claude/SKILL.md  + common/*
  dist/copilot-session-analyzer/  = copilot/SKILL.md + common/*

Usage:
  python build.py                       build + validate dist/
  python build.py --install             also copy to ~/.claude/skills and ~/.copilot/skills
  python build.py --install-repo PATH   also copy to PATH/.claude/skills and PATH/.github/skills
  python build.py --only copilot ...    restrict to one platform (claude | copilot)
"""

import argparse
import os
import re
import shutil
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(ROOT, "dist")
COMMON = os.path.join(ROOT, "common")

# platform folder -> (skill name, personal install dir, repo install dir)
SKILLS = {
    "claude": ("claude-session-analyzer", os.path.join("~", ".claude", "skills"), os.path.join(".claude", "skills")),
    "copilot": ("copilot-session-analyzer", os.path.join("~", ".copilot", "skills"), os.path.join(".github", "skills")),
}
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "evals", ".DS_Store")


def frontmatter(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return None, text
    fields = dict(re.findall(r"^([a-z-]+):\s*(.*)$", m.group(1), re.M))
    return fields, text


def validate(skill_dir, name):
    errors = []
    fields, text = frontmatter(os.path.join(skill_dir, "SKILL.md"))
    if fields is None:
        return ["SKILL.md: missing YAML frontmatter"]
    if fields.get("name") != name:
        errors.append("name %r must equal the folder name %r" % (fields.get("name"), name))
    if not re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", name) or len(name) > 64:
        errors.append("name must be kebab-case, <= 64 chars")
    desc = fields.get("description", "")
    if not desc:
        errors.append("description is required")
    if len(desc) > 1024:
        errors.append("description is %d chars (max 1024)" % len(desc))
    if "<" in desc or ">" in desc:
        errors.append("description must not contain angle brackets")
    lines = text.count("\n")
    if lines > 500:
        errors.append("SKILL.md is %d lines (keep it under 500)" % lines)
    for ref in sorted(set(re.findall(r"`((?:scripts|references)/[\w./-]+)`", text))):
        if not os.path.exists(os.path.join(skill_dir, ref)):
            errors.append("SKILL.md references missing file %s" % ref)
    return errors


def build(platform):
    name = SKILLS[platform][0]
    out = os.path.join(DIST, name)
    if os.path.isdir(out):
        shutil.rmtree(out)  # dist/ is generated output
    shutil.copytree(COMMON, out, ignore=IGNORE)
    src = os.path.join(ROOT, platform)
    for entry in os.listdir(src):
        if entry == "evals":
            continue
        s, d = os.path.join(src, entry), os.path.join(out, entry)
        if os.path.isdir(s):
            shutil.copytree(s, d, dirs_exist_ok=True, ignore=IGNORE)  # platform files may add or override
        else:
            shutil.copy2(s, d)
    return name, out


def install(src, target_parent, name):
    target_parent = os.path.expanduser(target_parent)
    target = os.path.join(target_parent, name)
    os.makedirs(target_parent, exist_ok=True)
    if os.path.isdir(target):
        shutil.rmtree(target)
        print("  replaced %s" % target)
    else:
        print("  installed %s" % target)
    shutil.copytree(src, target)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", choices=sorted(SKILLS))
    ap.add_argument("--install", action="store_true", help="copy to the personal skill folders")
    ap.add_argument("--install-repo", metavar="PATH", help="copy into a repository (.claude/skills, .github/skills)")
    args = ap.parse_args()

    failed = False
    for platform in ([args.only] if args.only else sorted(SKILLS)):
        name, out = build(platform)
        errors = validate(out, name)
        status = "OK" if not errors else "INVALID"
        print("%-26s %s  (%s)" % (name, status, os.path.relpath(out, ROOT)))
        for e in errors:
            print("  - " + e)
        if errors:
            failed = True
            continue
        if args.install:
            install(out, SKILLS[platform][1], name)
        if args.install_repo:
            install(out, os.path.join(args.install_repo, SKILLS[platform][2]), name)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
