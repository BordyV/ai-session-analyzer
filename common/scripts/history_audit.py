#!/usr/bin/env python3
"""history_audit.py - audit local AI-assistant chat history.

Shared by the `claude-session-analyzer` and `copilot-session-analyzer` skills.

Sources supported (read-only, nothing is ever sent over the network):
  claude       Claude Code transcripts       ~/.claude/projects/<project>/<session>.jsonl
  copilot-cli  GitHub Copilot CLI sessions   ~/.copilot/session-state/<id>/events.jsonl
                                              (+ ~/.copilot/session-store.db as fallback)
  vscode       Copilot Chat in VS Code        <VS Code user dir>/workspaceStorage/*/chatSessions/*.json[l]
  copilot      = copilot-cli + vscode
  all          = everything above

The script does the counting; the agent does the judgment. It produces a compact digest:
  - friction per session ("tours"): rounds, corrections, rephrasings, interruptions,
    rejected tool calls, tool errors, downvotes
  - recurring requests across sessions (clusters of similar prompts)
  - recurring shell commands
  - most frequent correction phrasings
  - slash commands / skills already used, skills and instruction files already present

Examples:
  python history_audit.py --list-sources
  python history_audit.py --source claude --days 30
  python history_audit.py --source copilot --days 14 --project my-repo --out digest.md
  python history_audit.py --source claude --show-session 3f2a
  python history_audit.py --source all --format json --out digest.json

Only the Python standard library is used (3.8+).
"""

import argparse
import bisect
import datetime as dt
import functools
import glob
import hashlib
import json
import math
import os
import pathlib
import re
import sqlite3
import sys
import unicodedata
from collections import Counter, defaultdict
from urllib.parse import unquote, urlparse

HOME = os.path.expanduser("~")
COPILOT_SOURCES = {"copilot-cli", "vscode", "jetbrains"}
NOW = dt.datetime.now(dt.timezone.utc)


# --------------------------------------------------------------------------- model

class Turn:
    """One user request and everything the assistant did until the next request."""

    def __init__(self, ts, text, kind="prompt"):
        self.ts = ts
        self.text = text
        self.kind = kind  # "prompt" (typed request) or "command" (slash command)
        self.tools = Counter()
        self.shell = []
        self.inputs = []  # (tool, fingerprint, text) of substantial non-shell tool inputs
        self.error_tools = Counter()
        self.error_samples = []  # (tool, error text, shell command or "")
        self.reply = ""  # last assistant text of the turn
        self.guardrails = []  # [(category, phrase, offset)] found by session_metrics
        self.defects = []
        self.tool_errors = 0
        self.rejections = 0
        self.interrupted = False
        self.downvote = False
        self.failed = False
        self.flags = []
        self.tokens = []


class Session:
    def __init__(self, source, sid, path):
        self.source = source
        self.client = source
        self.id = sid
        self.path = path
        self.project = None
        self.cwd = None
        self.cwds = set()  # every working directory seen during the session
        self.branch = None
        self.title = None
        self.start = None
        self.end = None
        self.turns = []
        self.commands = Counter()
        self.skills = Counter()
        self.metrics = {}
        self.foreign = []  # (ts, "assistant" | "utilisateur", excerpt): changes attributed to another session

    def touch(self, ts):
        if ts is None:
            return
        if self.start is None or ts < self.start:
            self.start = ts
        if self.end is None or ts > self.end:
            self.end = ts

    def current(self):
        return self.turns[-1] if self.turns else None

    def prompts(self):
        return [t for t in self.turns if t.kind == "prompt"]


# --------------------------------------------------------------------------- helpers

def parse_ts(value):
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            if value > 1e12:
                value = value / 1000.0
            return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc)
        s = str(value).strip()
        if s.isdigit():
            return parse_ts(int(s))
        s = s.replace("Z", "+00:00")
        # Python < 3.11 only accepts 3 or 6 fractional digits.
        s = re.sub(r"(\.\d{6})\d+", r"\1", s)
        d = dt.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d
    except (ValueError, OverflowError, OSError):
        return None


def fmt_day(ts):
    return ts.astimezone().strftime("%Y-%m-%d") if ts else "?"


def fmt_dt(ts):
    return ts.astimezone().strftime("%Y-%m-%d %H:%M") if ts else "?"


def first_str(d, *keys):
    if not isinstance(d, dict):
        return None
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return None


def flatten_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(flatten_text(c) for c in content)
    if isinstance(content, dict):
        return flatten_text(content.get("text") or content.get("content") or "")
    return ""


def read_jsonl(path):
    try:
        f = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                yield obj


def read_simple_yaml(path):
    """Flat `key: value` reader, enough for Copilot's workspace.yaml."""
    out = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*?)\s*$", line)
                if m and m.group(2):
                    out[m.group(1)] = m.group(2).strip("'\"")
    except OSError:
        pass
    return out


SECRET_RES = [
    re.compile(r"\b(sk-[A-Za-z0-9_-]{16,}|sk-ant-[A-Za-z0-9_-]{16,})"),
    re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key)\s*[:=]\s*\S+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
]
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def redact(text):
    for rx in SECRET_RES:
        text = rx.sub("<secret>", text)
    return EMAIL_RE.sub("<email>", text)


def excerpt(text, limit):
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return redact(text)


SYSTEM_BLOCK_RE = re.compile(
    r"<(system-reminder|local-command-caveat|local-command-stdout|local-command-stderr|"
    r"command-message|command-args|task-notification|ide_selection|ide_opened_file)\b[^>]*>.*?</\1>",
    re.S,
)


def clean_prompt(text):
    return SYSTEM_BLOCK_RE.sub(" ", text or "").strip()


# Work found in the folder that the assistant did not do: the trace of sessions running in parallel on the
# same files. Matched on the accent-free, lower-case text.
FOREIGN_RE = re.compile(
    r"\b(une|l'|par une|d'une|dans une) ?autre session (claude )?(a |l'a|les a|qui|aurait|avait|en parallele|ouverte|"
    r"tourne|a peut-etre|l'a peut-etre)"
    r"|\banother (claude )?session (has|is|was|did|might|may|made|modified|changed|running)"
    r"|\b(ne viennent|ne vient) pas de moi|\bpas (ete )?(fait|faite|faites|ecrit|ecrite|ecrites|modifiee?s?) par moi"
    r"|\b(modifications?|changements?) (que je n'ai pas (faite?s?|ecrite?s?)|que je ne reconnais pas)\b"
    r"|\bpar quelqu'un d'autre que moi|\bquelqu'un d'autre (a|que moi)\b"
    r"|\bc'est toi (qui|ou)\b.{0,60}\bautre session"
    r"|\bnot (made|written|done) by me\b|\bi didn'?t (make|write) (these|those|this)\b"
    r"|\bchanges? (that|which) (aren'?t|are not|weren'?t) mine\b")


def note_foreign(s, ts, who, text):
    """Remember mentions of changes made by another session (or someone else) in the same folder."""
    if not text or len(s.foreign) >= 20:
        return
    n = normalize(text)
    m = FOREIGN_RE.search(n)
    if m:
        start = max(0, m.start() - 50)
        s.foreign.append((ts, who, ("…" if start else "") + excerpt(n[start:], 170)))


# --------------------------------------------------------------------------- Claude Code

CLAUDE_BUILTIN_COMMANDS = set(
    "add-dir agents artifacts bug clear compact config context cost doctor effort exit export fast "
    "feedback help hooks ide init install-github-app login logout mcp memory model output-style "
    "permissions plugin plugins privacy-settings quit release-notes resume rewind status statusline "
    "terminal-setup theme upgrade usage vim".split()
)
CLAUDE_SKIP_PREFIXES = (
    "<local-command-caveat>", "<local-command-stdout>", "<local-command-stderr>",
    "<task-notification>", "<system-reminder>", "Caveat:",
    "This session is being continued from a previous conversation",
)
CLAUDE_REJECT_MARKERS = (
    "The user doesn't want to proceed with this tool use",
    "doesn't want to take this action",
    "User rejected",
)
COMMAND_NAME_RE = re.compile(r"<command-name>\s*/?([^<\s]+)\s*</command-name>")
COMMAND_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.S)
INTERRUPT_RE = re.compile(r"^\[Request interrupted by user")
SHELL_TOOLS = {"bash", "powershell", "shell", "run_in_terminal", "runinterminal", "terminal",
               "run_terminal_command", "execute_command", "run_command"}


# Tools whose input is file content, a search or plumbing: repeating them says nothing about a procedure.
NON_PROCEDURAL_TOOLS = {
    "write", "edit", "multiedit", "notebookedit", "read", "glob", "grep", "todowrite", "task", "agent",
    "webfetch", "websearch", "toolsearch", "skill", "askuserquestion", "exitplanmode", "sendmessage",
    "create_file", "replace_string_in_file", "insert_edit_into_file", "read_file", "list_dir", "file_search",
    "grep_search", "semantic_search", "edit", "create", "view", "str_replace_editor", "glob", "rg", "task",
    "ask_user", "read_agent", "list_agents", "report_intent", "update_todo", "fetch", "web_fetch",
}


def tool_family(name):
    """mcp__claude-in-chrome__computer -> mcp:claude-in-chrome; other tools keep their name."""
    if name.startswith("mcp__"):
        parts = name.split("__")
        return "mcp:" + (parts[1] if len(parts) > 2 else name[5:])
    return name


def record_tool_input(turn, name, inp):
    """Remember substantial tool inputs (code snippets, scripts, queries) to spot ones rewritten again and again."""
    if not isinstance(inp, dict) or name.lower() in SHELL_TOOLS or name.lower() in NON_PROCEDURAL_TOOLS:
        return
    text = max((v for v in inp.values() if isinstance(v, str)), key=len, default="")
    if len(text) < 40:
        return
    norm = re.sub(r"\d+", "0", re.sub(r"\s+", " ", text)).strip()[:4000]
    turn.inputs.append((name, hashlib.sha1(norm.encode("utf-8", "replace")).hexdigest()[:12], text))


def claude_user_text(s, text, ts):
    raw = (text or "").strip()
    if not raw:
        return
    if INTERRUPT_RE.match(raw):
        cur = s.current()
        if cur:
            cur.interrupted = True
        return
    m = COMMAND_NAME_RE.search(raw)
    if m:
        name = m.group(1)
        s.commands[name] += 1
        if name.lower() not in CLAUDE_BUILTIN_COMMANDS:
            args = COMMAND_ARGS_RE.search(raw)
            arg = args.group(1).strip() if args else ""
            s.turns.append(Turn(ts, ("/" + name + " " + arg).strip(), kind="command"))
        return
    if raw.startswith(CLAUDE_SKIP_PREFIXES):
        return
    txt = clean_prompt(raw)
    if txt:
        s.turns.append(Turn(ts, txt))
        note_foreign(s, ts, "utilisateur", txt)


def load_claude(claude_home):
    for path in sorted(glob.glob(os.path.join(claude_home, "projects", "*", "*.jsonl"))):
        s = Session("claude", os.path.splitext(os.path.basename(path))[0], path)
        project_dir = os.path.basename(os.path.dirname(path))
        tool_ids = {}  # tool_use id -> (tool name, shell command), to attribute errors
        for o in read_jsonl(path):
            t = o.get("type")
            if t == "ai-title" and o.get("aiTitle"):
                s.title = o["aiTitle"]
                continue
            if t == "summary" and o.get("summary") and not s.title:
                s.title = o["summary"]
                continue
            if t not in ("user", "assistant") or o.get("isSidechain"):
                continue
            ts = parse_ts(o.get("timestamp"))
            s.touch(ts)
            s.cwd = s.cwd or o.get("cwd")
            if o.get("cwd"):
                s.cwds.add(o["cwd"])
            s.branch = s.branch or o.get("gitBranch")
            msg = o.get("message") if isinstance(o.get("message"), dict) else {}
            content = msg.get("content")

            if t == "assistant":
                cur = s.current()
                if cur is None or not isinstance(content, list):
                    continue
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text" and (b.get("text") or "").strip():
                        cur.reply = b["text"]
                        note_foreign(s, ts, "assistant", b["text"])
                    if b.get("type") != "tool_use":
                        continue
                    name = b.get("name") or "?"
                    cur.tools[name] += 1
                    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                    cmd = inp.get("command") if name.lower() in SHELL_TOOLS and isinstance(inp.get("command"), str) else ""
                    tool_ids[b.get("id")] = (name, cmd)
                    if cmd:
                        cur.shell.append(cmd)
                    if name == "Skill" and inp.get("skill"):
                        s.skills[str(inp["skill"])] += 1
                    record_tool_input(cur, name, inp)
                continue

            # user record
            if o.get("isMeta"):
                continue
            origin = o.get("origin")
            if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
                continue
            denial = o.get("toolDenialKind")
            if isinstance(content, list):
                texts = []
                counted_reject = False
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_result":
                        cur = s.current()
                        if cur is None or not b.get("is_error"):
                            continue
                        body = flatten_text(b.get("content"))
                        if denial == "user-rejected" or any(mk in body for mk in CLAUDE_REJECT_MARKERS):
                            if not counted_reject:
                                cur.rejections += 1
                                counted_reject = True
                        elif denial in (None, ""):
                            tool, cmd = tool_ids.get(b.get("tool_use_id"), ("?", ""))
                            cur.tool_errors += 1
                            cur.error_tools[tool] += 1
                            cur.error_samples.append((tool, body, cmd))
                    elif b.get("type") == "text":
                        texts.append(b.get("text") or "")
                if texts:
                    claude_user_text(s, "\n".join(texts), ts)
            elif isinstance(content, str):
                claude_user_text(s, content, ts)

        s.project = project_name(s.cwd) or project_dir
        yield s


def project_name(cwd):
    if not cwd:
        return None
    return os.path.basename(cwd.rstrip("\\/")) or cwd


# --------------------------------------------------------------------------- Copilot CLI

def generic_prompt(s, text, ts):
    txt = clean_prompt(text or "")
    if not txt:
        return
    m = re.match(r"^/([\w.:-]+)", txt)
    if m:
        s.commands[m.group(1)] += 1
    s.turns.append(Turn(ts, txt))
    note_foreign(s, ts, "utilisateur", txt)


def shell_command(tool_name, args):
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return None
    if not isinstance(args, dict):
        return None
    cmd = args.get("command") or args.get("commandLine") or args.get("cmd")
    if isinstance(cmd, dict):
        cmd = cmd.get("original")
    if isinstance(cmd, str) and (tool_name or "").lower() in SHELL_TOOLS | {"bash", "powershell"}:
        return cmd
    if isinstance(cmd, str) and "terminal" in (tool_name or "").lower():
        return cmd
    return None


UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def copilot_client_source(client_name):
    """Copilot CLI's session store is shared: IntelliJ and VS Code agent sessions land there too."""
    c = (client_name or "").lower()
    if any(k in c for k in ("intellij", "jetbrains", "pycharm", "idea", "rider", "webstorm", "goland")):
        return "jetbrains"
    if "vscode" in c or "vs-code" in c or "code-insiders" in c:
        return "vscode"
    return "copilot-cli"


def load_copilot_cli(copilot_home):
    seen = set()
    for d in sorted(glob.glob(os.path.join(copilot_home, "session-state", "*"))):
        events = os.path.join(d, "events.jsonl")
        if not os.path.isfile(events):
            continue
        sid = os.path.basename(d)
        meta = read_simple_yaml(os.path.join(d, "workspace.yaml"))
        s = Session(copilot_client_source(meta.get("client_name")), sid, events)
        s.client = meta.get("client_name") or "copilot-cli"
        s.cwd = meta.get("cwd") or meta.get("git_root")
        s.branch = meta.get("branch")
        name = meta.get("name")
        s.title = meta.get("summary") or (name if name and not UUID_RE.match(name) else None)
        s.touch(parse_ts(meta.get("created_at")))
        repo = meta.get("repository")
        tool_ids, denied = {}, set()
        for o in read_jsonl(events):
            etype = str(o.get("type") or o.get("kind") or "")
            data = o.get("data") if isinstance(o.get("data"), dict) else o
            ts = parse_ts(o.get("timestamp") or data.get("timestamp"))
            s.touch(ts)
            cur = s.current()
            if etype == "session.start":
                ctx = data.get("context") if isinstance(data.get("context"), dict) else data
                s.cwd = s.cwd or first_str(ctx, "cwd", "workingDirectory", "gitRoot")
                s.branch = s.branch or first_str(ctx, "branch", "gitBranch")
                repo = repo or first_str(ctx, "repository")
            elif etype == "user.message":
                source = str(data.get("source") or "")
                # Prompts written by a sub-agent ("agent-<id>") or the system are not the user's.
                if o.get("agentId") or source in ("system", "hook") or source.startswith("agent"):
                    continue
                generic_prompt(s, first_str(data, "content", "text", "message", "prompt"), ts)
            elif etype == "assistant.message" and cur is not None:
                text = first_str(data, "content", "text", "message")
                # Sub-agent chatter (parentToolCallId) is not what the user read.
                if text and not data.get("parentToolCallId") and not o.get("agentId"):
                    cur.reply = text
                    note_foreign(s, ts, "assistant", text)
            elif etype == "permission.completed" and cur is not None:
                result = data.get("result") if isinstance(data.get("result"), dict) else {}
                kind = str(result.get("kind") or "").lower()
                if kind and not kind.startswith("approv"):
                    cur.rejections += 1
                    denied.add(data.get("toolCallId"))
            elif etype == "tool.execution_start" and cur is not None:
                name = first_str(data, "toolName", "name", "tool") or "?"
                cur.tools[name] += 1
                args = data.get("arguments") or data.get("args") or data.get("input")
                cmd = shell_command(name, args)
                tool_ids[data.get("toolCallId")] = (name, cmd or "")
                if cmd:
                    cur.shell.append(cmd)
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = None
                record_tool_input(cur, name, args)
            elif etype == "tool.execution_complete" and cur is not None:
                err = data.get("error")
                if (data.get("success") is False or err) and data.get("toolCallId") not in denied:
                    detail = err if err else data.get("result")
                    text = flatten_text(detail.get("message") or detail.get("content")) if isinstance(detail, dict) \
                        else str(detail or "")
                    if re.search(r"(?i)\b(denied|rejected|declined|cancel)", text[:500]):
                        cur.rejections += 1
                    else:
                        tool, cmd = tool_ids.get(data.get("toolCallId"), (first_str(data, "toolName") or "?", ""))
                        cur.tool_errors += 1
                        cur.error_tools[tool] += 1
                        cur.error_samples.append((tool, text, cmd))
            elif etype in ("abort", "session.abort", "user.abort", "turn.abort") and cur is not None:
                cur.interrupted = True
            elif etype == "skill.invoked":
                name = first_str(data, "name", "skillName", "skill")
                if name:
                    s.skills[name] += 1
            elif etype == "session.task_complete" and not s.title:
                s.title = excerpt(first_str(data, "summary") or "", 100) or None
        # Same naming as the other sources (folder name), so one repo is one project across tools.
        s.project = project_name(s.cwd) or project_name(repo)
        seen.add(sid)
        yield s

    # Fallback: the session store (sessions whose raw events are gone).
    db = os.path.join(copilot_home, "session-store.db")
    if not os.path.isfile(db):
        return
    try:
        con = sqlite3.connect(pathlib.Path(db).resolve().as_uri() + "?mode=ro", uri=True)
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"sessions", "turns"} <= tables:
            return
        cols = {r[1] for r in con.execute("PRAGMA table_info(sessions)")}
        want = [c for c in ("id", "cwd", "repository", "branch", "summary", "created_at") if c in cols]
        sessions = {}
        for row in con.execute("SELECT %s FROM sessions" % ", ".join(want)):
            rec = dict(zip(want, row))
            if rec.get("id") in seen:
                continue
            s = Session("copilot-cli", rec.get("id"), db)
            s.cwd, s.branch, s.title = rec.get("cwd"), rec.get("branch"), rec.get("summary")
            s.project = project_name(s.cwd) or project_name(rec.get("repository"))
            s.touch(parse_ts(rec.get("created_at")))
            sessions[s.id] = s
        for sid, _idx, msg, ts in con.execute(
                "SELECT session_id, turn_index, user_message, timestamp FROM turns ORDER BY session_id, turn_index"):
            s = sessions.get(sid)
            if s is not None:
                t = parse_ts(ts)
                s.touch(t)
                generic_prompt(s, msg, t)
        con.close()
        for s in sessions.values():
            yield s
    except sqlite3.Error as e:
        print("warning: cannot read %s (%s)" % (db, e), file=sys.stderr)


# --------------------------------------------------------------------------- VS Code Copilot Chat

def vscode_user_dirs(override=None):
    if override:
        return [override]
    flavors = ["Code", "Code - Insiders", "VSCodium"]
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA", os.path.join(HOME, "AppData", "Roaming"))
    elif sys.platform == "darwin":
        base = os.path.join(HOME, "Library", "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME", os.path.join(HOME, ".config"))
    dirs = [os.path.join(base, f, "User") for f in flavors]
    # Remote / WSL servers keep their own copy.
    dirs += [os.path.join(HOME, ".vscode-server", "data", "User")]
    return [d for d in dirs if os.path.isdir(d)]


def _container(state, path):
    cur = state
    for i, key in enumerate(path):
        nxt_is_index = i + 1 < len(path) and isinstance(path[i + 1], int)
        if isinstance(cur, list) and isinstance(key, int):
            while len(cur) <= key:
                cur.append(None)
            if cur[key] is None:
                cur[key] = [] if nxt_is_index else {}
            cur = cur[key]
        elif isinstance(cur, dict):
            if cur.get(key) is None:
                cur[key] = [] if nxt_is_index else {}
            cur = cur[key]
        else:
            return None
    return cur


def _set(state, path, value, delete=False):
    if not path:
        return
    parent = _container(state, path[:-1])
    key = path[-1]
    if isinstance(parent, list) and isinstance(key, int):
        while len(parent) <= key:
            parent.append(None)
        parent[key] = None if delete else value
    elif isinstance(parent, dict):
        if delete:
            parent.pop(key, None)
        else:
            parent[key] = value


def _push(state, path, items, index):
    arr = _container(state, path) if path else state
    if not isinstance(arr, list):
        _set(state, path, [])
        arr = _container(state, path)
    if not isinstance(arr, list):
        return
    if isinstance(index, int):
        del arr[index:]
    if isinstance(items, list):
        arr.extend(items)


def replay_mutation_log(path):
    """VS Code >= 1.109 stores a chat as an object mutation log (objectMutationLog.ts):
    kind 0 = initial state, 1 = set k to v, 2 = push v onto array k (truncate at i first), 3 = delete k."""
    state = None
    for e in read_jsonl(path):
        kind = e.get("kind")
        if kind == 0:
            state = e.get("v")
        elif state is None:
            continue
        elif kind == 1:
            _set(state, e.get("k") or [], e.get("v"))
        elif kind == 2:
            _push(state, e.get("k") or [], e.get("v"), e.get("i"))
        elif kind == 3:
            _set(state, e.get("k") or [], None, delete=True)
    return state


def vscode_workspace(ws_dir):
    try:
        with open(os.path.join(ws_dir, "workspace.json"), encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return None, None
    uri = meta.get("folder") or meta.get("workspace")
    if not uri:
        return None, None
    path = unquote(urlparse(uri).path)
    if re.match(r"^/[A-Za-z]:", path):
        path = path[1:]
    name = os.path.basename(path.rstrip("/\\"))
    if name.endswith(".code-workspace"):
        name = name[: -len(".code-workspace")]
    return name, path


def vscode_session(state, sid, path, project, cwd):
    s = Session("vscode", state.get("sessionId") or sid, path)
    s.project, s.cwd = project, cwd
    s.title = state.get("customTitle") or state.get("computedTitle")
    s.touch(parse_ts(state.get("creationDate")))
    for r in state.get("requests") or []:
        if not isinstance(r, dict) or r.get("isSystemInitiated"):
            continue
        msg = r.get("message")
        text = msg if isinstance(msg, str) else (msg.get("text") if isinstance(msg, dict) else "")
        ts = parse_ts(r.get("timestamp"))
        s.touch(ts)
        before = len(s.turns)
        generic_prompt(s, text, ts)
        if len(s.turns) == before:
            continue
        cur = s.current()
        if r.get("vote") == 0:
            cur.downvote = True
        model_state = r.get("modelState")
        state_value = model_state.get("value") if isinstance(model_state, dict) else None
        if r.get("isCanceled") or state_value == 2:
            cur.interrupted = True
        result = r.get("result") if isinstance(r.get("result"), dict) else {}
        if state_value == 3 or result.get("errorDetails"):
            cur.failed = True
        markdown = []
        for p in r.get("response") or []:
            if not isinstance(p, dict):
                continue
            if p.get("kind") in (None, "markdownContent", "markdownVuln"):
                value = p.get("value") if isinstance(p.get("value"), str) else (p.get("content") or {}).get("value") \
                    if isinstance(p.get("content"), dict) else None
                if isinstance(value, str):
                    markdown.append(value)
            if p.get("kind") != "toolInvocationSerialized":
                continue
            tool = p.get("toolId") or "?"
            cur.tools[tool] += 1
            data = p.get("toolSpecificData") if isinstance(p.get("toolSpecificData"), dict) else {}
            cmd = ""
            if data.get("kind") == "terminal":
                cl = data.get("commandLine")
                cmd = cl.get("original") if isinstance(cl, dict) else cl
                cmd = cmd if isinstance(cmd, str) else ""
                if cmd:
                    cur.shell.append(cmd)
            if p.get("resultError"):
                cur.tool_errors += 1
                cur.error_tools[tool] += 1
                cur.error_samples.append((tool, flatten_text(p.get("resultError")), cmd))
            confirmed = p.get("isConfirmed")
            # ToolConfirmKind.Denied = 0, Skipped = 5; pre-1.104 used a boolean.
            if confirmed is False or (isinstance(confirmed, dict) and confirmed.get("type") in (0, 5)):
                cur.rejections += 1
        if markdown:
            cur.reply = "".join(markdown)
            note_foreign(s, ts, "assistant", cur.reply)
    return s


def load_vscode(user_dir_override=None):
    for user_dir in vscode_user_dirs(user_dir_override):
        groups = []
        for ws in glob.glob(os.path.join(user_dir, "workspaceStorage", "*")):
            files = glob.glob(os.path.join(ws, "chatSessions", "*.json*"))
            if files:
                groups.append((vscode_workspace(ws), files))
        empty = glob.glob(os.path.join(user_dir, "globalStorage", "emptyWindowChatSessions", "*.json*"))
        if empty:
            groups.append(((None, None), empty))
        for (project, cwd), files in groups:
            by_id = {}
            for f in files:
                sid, ext = os.path.splitext(os.path.basename(f))
                if ext not in (".json", ".jsonl"):
                    continue
                if sid not in by_id or ext == ".jsonl":
                    by_id[sid] = f
            for sid, f in by_id.items():
                try:
                    if f.endswith(".jsonl"):
                        state = replay_mutation_log(f)
                    else:
                        with open(f, encoding="utf-8", errors="replace") as fh:
                            state = json.load(fh)
                except (OSError, ValueError):
                    continue
                if isinstance(state, dict):
                    yield vscode_session(state, sid, f, project or "(sans dossier)", cwd)


# --------------------------------------------------------------------------- JetBrains Copilot plugin (best effort)
#
# The plugin (IntelliJ IDEA, PyCharm, …) keeps its own chats in Nitrite databases: H2 MVStore files of
# Java-serialized documents, an undocumented binary format. As codeburn does
# (github.com/getagentseal/codeburn, src/providers/copilot.ts), the file is read as latin-1 text and the
# JSON records embedded in it are recovered by progressive unescaping. In agent mode the `Markdown`
# records hold the user's prompts and `AgentRound` records the replies; in ask mode `Markdown` holds the
# reply and the prompt location is unknown, so those turns are only counted. Chats the plugin runs through
# Copilot CLI (`copilot --acp`) land in ~/.copilot/session-store.db and are read by load_copilot_cli.

GUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
JB_CONVERSATION_RE = re.compile(r"\$(" + GUID + r")[\s\S]{0,8}name")
JB_TITLE_RE = re.compile(r"value.{1,6}?([\x20-\x7e]{3,80}?)t\x00\x06source", re.S)
JB_BLOB_RE = re.compile(r'\{"__first__":\s*\{"type":\s*"Subgraph"')
JB_LEGACY_RE = re.compile(r'[\x00-\x1f\x7f-\xff]\{"' + GUID + r'":\s*\{"type":\s*"Value"')
JB_PROJECT_RE = re.compile(r"projectName\x74([\x00-\xff])([\x00-\xff])")
JB_DATA_RE = re.compile(r'"data":\s*"')
JB_AGENT_RE = re.compile(r'"type":\s*"AgentRound"')
JB_MARKDOWN_RE = re.compile(r'"type":\s*"Markdown"')
JB_ERROR_RE = re.compile(r'error occurred|\\*"isError\\*":true|\\*"type\\*":\\*"Error\\*"', re.I)
JB_DEFAULT_TITLES = {"New Agent Session", "New Session", "New Chat"}
JB_KINDS = ("chat-agent-sessions", "chat-sessions", "chat-edit-sessions")
JB_MAX_BYTES = 300 * 1024 * 1024


def jetbrains_root(override=None):
    if override:
        return override
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg and os.path.isabs(xdg):
        return os.path.join(xdg, "github-copilot")
    if sys.platform.startswith("win"):
        return os.path.join(os.environ.get("LOCALAPPDATA", os.path.join(HOME, "AppData", "Local")), "github-copilot")
    return os.path.join(HOME, ".config", "github-copilot")


def jetbrains_dbs(root):
    found = []
    for kind in JB_KINDS:
        found += glob.glob(os.path.join(root, "*", kind, "*", "*nitrite*.db"))
    return sorted(found)


def latin1_to_utf8(text):
    """Undo the latin-1 reading of UTF-8 bytes; leave text that was not mangled (e.g. JSON \\u escapes) as is."""
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def _match_json_object(raw, start):
    depth, in_str, esc, i = 0, False, False, start
    while i < len(raw):
        c = raw[i]
        i += 1
        if esc:
            esc = False
        elif c == "\\":
            esc = True
        elif c == '"':
            in_str = not in_str
        elif not in_str:
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    break
    return raw[start:i], i


def _record_docs(s, record_type):
    """Parse the one-level-escaped `data` document of every bare `"type":"<record_type>"` record in s."""
    docs = []
    for m in re.finditer(r'"type":\s*"%s"' % record_type, s):
        dm = JB_DATA_RE.search(s, m.start(), m.start() + 220)
        if not dm:
            continue
        start = i = dm.end()
        while i < len(s):
            if s[i] == '"':
                backslashes, j = 0, i - 1
                while j >= start and s[j] == "\\":
                    backslashes += 1
                    j -= 1
                if backslashes % 2 == 0:
                    break
            i += 1
        try:
            doc = json.loads(json.loads('"' + s[start:i] + '"', strict=False), strict=False)
        except (ValueError, TypeError):
            continue
        if isinstance(doc, dict):
            docs.append(doc)
    return docs


def jetbrains_blob(chunk):
    """Return (prompts, replies, tool_calls, errored) for one turn blob, unescaping until records appear bare."""
    s = chunk
    for _ in range(8):
        agent = JB_AGENT_RE.search(s) is not None
        if agent or JB_MARKDOWN_RE.search(s):
            texts = [d.get("text") for d in _record_docs(s, "Markdown") if isinstance(d.get("text"), str)]
            rounds = _record_docs(s, "AgentRound")
            replies = [d.get("reply") for d in rounds if isinstance(d.get("reply"), str) and d.get("reply")]
            calls = sum(len(d.get("toolCalls") or []) for d in rounds if isinstance(d.get("toolCalls"), list))
            prompts = texts if agent else []  # ask mode: Markdown is the reply
            if not agent:
                replies = texts
            return ([latin1_to_utf8(p) for p in prompts if p.strip()], replies, calls, bool(JB_ERROR_RE.search(chunk)))
        nxt = re.sub(r'\\([\\"])', r"\1", s)
        if nxt == s:
            break
        s = nxt
    return [], [], 0, bool(JB_ERROR_RE.search(chunk))


def jetbrains_conversations(raw):
    titles = {}
    for m in JB_CONVERSATION_RE.finditer(raw):
        cid = m.group(1).lower()
        tm = JB_TITLE_RE.search(raw[m.start(): m.start() + 400])
        if not tm:
            titles.setdefault(cid, None)
            continue
        title = latin1_to_utf8(re.sub(r"^[^A-Za-z0-9]*", "", tm.group(1))).strip()
        if title and not (titles.get(cid) and title in JB_DEFAULT_TITLES):
            titles[cid] = title
    return titles


def jetbrains_project_name(raw):
    for m in JB_PROJECT_RE.finditer(raw):
        n = (ord(m.group(1)) << 8) | ord(m.group(2))
        if 1 <= n <= 128:
            value = latin1_to_utf8(raw[m.end(): m.end() + n])
            if value and not re.search(r"[\x00-\x1f]", value):
                return value
    return None


def jetbrains_project_from_files(raw):
    """Walk up from referenced file:// paths to the nearest folder holding .git."""
    for m in re.finditer(r'file://(/[^"\\\s]+)', raw[:5_000_000]):
        path = unquote(m.group(1))
        if re.match(r"^/[A-Za-z]:/", path):
            path = path[1:]
        d = os.path.dirname(path)
        for _ in range(30):
            if not d or os.path.dirname(d) == d:
                break
            if os.path.exists(os.path.join(d, ".git")):
                return os.path.basename(d), d
            d = os.path.dirname(d)
    return None, None


def read_latin1(path):
    with open(path, "rb") as f:
        return f.read(JB_MAX_BYTES).decode("latin-1")


def jetbrains_blobs(raw):
    """Turn blobs: per-turn `__first__` subgraphs (plugin >= 1.12), else whole-session documents (<= 1.5)."""
    blobs = []
    for m in JB_BLOB_RE.finditer(raw):
        chunk, _end = _match_json_object(raw, m.start())
        blobs.append((m.start(), chunk))
    if not blobs and "AgentRound" in raw:
        for m in JB_LEGACY_RE.finditer(raw):
            chunk, _end = _match_json_object(raw, m.start() + 1)
            if "AgentRound" in chunk:
                blobs.append((m.start(), chunk))
    return blobs


WARNINGS = []  # problems met while reading, reported in the digest's limits


def jetbrains_lock_message(db, error):
    if isinstance(error, PermissionError) or getattr(error, "winerror", None) in (32, 33):
        return ("base native JetBrains verrouillée (IDE ouvert). Plugin récent : elle ne contient que des métadonnées, "
                "les conversations sont lues via ~/.copilot/session-state (source jetbrains), rien à faire. "
                "Plugin ancien : fermez l'IDE pour la lire.")
    return "base JetBrains illisible (%s) : %s" % (error, db)


def load_jetbrains(root_override=None):
    root = jetbrains_root(root_override)
    sessions = {}
    for db in jetbrains_dbs(root):
        try:
            raw = read_latin1(db)
            mtime = dt.datetime.fromtimestamp(os.path.getmtime(db), tz=dt.timezone.utc)
        except OSError as e:
            WARNINGS.append(jetbrains_lock_message(db, e))
            continue
        rel = os.path.relpath(db, root).replace("\\", "/").split("/")
        ide, store = rel[0], rel[2]
        titles = jetbrains_conversations(raw)
        # A turn belongs to the conversation whose id appears last before it in the file.
        anchors = sorted((m.start(), m.group(0).lower()) for m in re.finditer(GUID, raw)
                         if m.group(0).lower() in titles) if titles else []
        anchor_pos = [p for p, _ in anchors]
        project = jetbrains_project_name(raw)
        cwd = None
        if not project:
            project, cwd = jetbrains_project_from_files(raw)
        for pos, chunk in jetbrains_blobs(raw):
            k = bisect.bisect_left(anchor_pos, pos)
            conv = anchors[k - 1][1] if k else None
            sid = conv or "%s-%s" % (ide, store)
            s = sessions.get(sid)
            if s is None:
                s = sessions[sid] = Session("jetbrains", sid, db)
                s.client = "jetbrains-nitrite"
                s.project = project or "jetbrains:" + ide
                s.cwd = cwd
                s.title = titles.get(conv) if conv else None
                s.touch(mtime)  # the store has no per-turn timestamps: file date only
                s.seen_prompts, s.seen_blobs = set(), set()
            if chunk in s.seen_blobs:
                continue  # MVStore keeps byte-identical copies of a record
            s.seen_blobs.add(chunk)
            prompts, replies, calls, errored = jetbrains_blob(chunk)
            for p in prompts:
                if p in s.seen_prompts:
                    continue  # MVStore keeps several copies / revisions of each record
                s.seen_prompts.add(p)
                generic_prompt(s, p, None)
            cur = s.current()
            if cur is not None and replies:
                cur.reply = latin1_to_utf8(replies[-1])
            if cur is not None and (prompts or calls):
                if calls:
                    cur.tools["agent-tool"] += calls
                if errored and not replies:
                    cur.failed = True
    return list(sessions.values())


def jetbrains_diagnose(root_override=None, copilot_home=None, excerpts=True):
    """Structure report to improve the JetBrains reader on a machine that has real data."""
    root = jetbrains_root(root_override)
    cli_sessions = {os.path.basename(p).lower() for p in
                    glob.glob(os.path.join(copilot_home or os.path.join(HOME, ".copilot"), "session-state", "*"))}
    L = ["# Diagnostic Copilot JetBrains", "", "Racine du plugin : %s (%s)" % (root, "existe" if os.path.isdir(root) else "absente")]
    if os.path.isdir(root):
        for ide in sorted(os.listdir(root)):
            p = os.path.join(root, ide)
            if os.path.isdir(p):
                L.append("- %s/ : %s" % (ide, ", ".join(sorted(os.listdir(p)))[:300]))
    for db in jetbrains_dbs(root):
        try:
            raw = read_latin1(db)
        except OSError as e:
            L.append("\n## %s\n- %s" % (os.path.relpath(db, root), jetbrains_lock_message(db, e)))
            continue
        blobs = jetbrains_blobs(raw)
        prompts, replies, calls, errors = [], 0, 0, 0
        for _pos, chunk in blobs:
            p, r, c, e = jetbrains_blob(chunk)
            prompts += p
            replies += len(r)
            calls += c
            errors += int(e)
        types = Counter(re.findall(r'\\*"type\\*":\\*"([A-Za-z]{2,40})\\*"', raw))
        entities = Counter(re.findall(r"com\.github\.copilot[\w.]*\.(Nt\w+)", raw))
        L += ["", "## %s" % os.path.relpath(db, root),
              "- taille : %d octets · en-tête : %s" % (os.path.getsize(db), re.sub(r"[^\x20-\x7e]", ".", raw[:50])),
              "- entités Nitrite : %s" % (", ".join("%s×%d" % kv for kv in entities.most_common(10)) or "aucune"),
              "- types de records JSON : %s" % (", ".join("%s×%d" % kv for kv in types.most_common(15)) or "aucun"),
              "- conversations : %d · blobs de tour : %d · demandes extraites : %d (uniques %d) · réponses : %d · appels d'outils : %d · erreurs : %d"
              % (len(jetbrains_conversations(raw)), len(blobs), len(prompts), len(set(prompts)), replies, calls, errors),
              "- projectName : %s" % (jetbrains_project_name(raw) or "absent")]
        linked = sorted({g.lower() for g in re.findall(GUID, raw)} & cli_sessions)
        if linked:
            L.append("- sessions Copilot CLI référencées : %d (%s)" % (len(linked), ", ".join(g[:8] for g in linked)))
            if not blobs:
                L.append("  → base de métadonnées seulement (plugin récent) : le texte des conversations est dans "
                         "~/.copilot/session-state et il est déjà lu (source jetbrains).")
        if excerpts:
            for p in list(dict.fromkeys(prompts))[:3]:
                L.append("  - « %s »" % excerpt(p, 70))
    jb_cli = os.path.join(copilot_home or os.path.join(HOME, ".copilot"), "jb")
    if os.path.isdir(jb_cli):
        L += ["", "## %s (plugin >= 1.9)" % jb_cli]
        for dirpath, _dirs, files in os.walk(jb_cli):
            for f in files[:30]:
                p = os.path.join(dirpath, f)
                L.append("- %s (%d octets)" % (os.path.relpath(p, jb_cli), os.path.getsize(p)))
    store = os.path.join(copilot_home or os.path.join(HOME, ".copilot"), "session-store.db")
    if os.path.isfile(store):
        try:
            con = sqlite3.connect(pathlib.Path(store).resolve().as_uri() + "?mode=ro", uri=True)
            cols = {r[1] for r in con.execute("PRAGMA table_info(sessions)")}
            if "host_type" in cols:
                hosts = Counter(r[0] for r in con.execute("SELECT host_type FROM sessions"))
                L += ["", "## session-store.db (sessions Copilot CLI, dont celles lancées par l'IDE)",
                      "- host_type : " + ", ".join("%s×%d" % kv for kv in hosts.most_common())]
            con.close()
        except sqlite3.Error as e:
            L.append("- session-store.db illisible : %s" % e)
    if not jetbrains_dbs(root) and not os.path.isdir(jb_cli):
        L.append("\nAucune base de chat JetBrains trouvée. Si le plugin passe par Copilot CLI, ses sessions sont lues via --source copilot-cli.")
    return "\n".join(L)


# --------------------------------------------------------------------------- text analysis

STOPWORDS = set("""
le la les un une des du de l d et ou en au aux ce cet cette ces se sa son ses mon ma mes ton ta tes
notre nos votre vos leur leurs je tu il elle on nous vous ils elles me te moi toi lui y est es suis
sont etre ai as avons avez ont avoir fait faire fais pour par sur dans avec sans sous qui que quoi
dont ne pas plus moins tres bien tout tous toute toutes comme mais donc car si alors aussi encore
deja juste peux peut veux veut voudrais faut quel quelle quels quelles cela ici oui non merci stp
svp ok okay voici voila celui celle ceux entre chaque autre autres meme dois doit
the and or of to in on for with without by from at as is are was were be been being it its this
that these those you he she we they my your our their do does did done can could would should will
shall may might must have has had not no yes just also too very so then than there here what which
who whom how why when where all any some each more most other into about over after before up down
out if else please thanks thank let lets
""".split())


def normalize(text):
    text = (text or "").replace("’", "'").replace("‘", "'")
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


# Action verbs that ask for the same thing ("génère / écris / crée les tests de X").
VERB_SYNONYMS = {w: "creer" for w in (
    "genere", "generer", "genere-moi", "ecris", "ecrire", "ecrit", "cree", "creer", "crees", "redige", "rediger",
    "produis", "produire", "generate", "write", "create", "draft")}
VERB_SYNONYMS.update({w: "corrig" for w in ("corrige", "corriger", "repare", "reparer", "fix", "fixe", "debug", "debugge")})
VERB_SYNONYMS.update({w: "explic" for w in ("explique", "expliquer", "explain", "decris", "decrire", "describe")})


def stem(word):
    word = VERB_SYNONYMS.get(word, word)
    if len(word) > 4 and word[-1] in "sx":
        word = word[:-1]
    return word[:7]


def tokenize(text, cap=60):
    t = normalize(text)
    t = re.sub(r"```.*?```", " codeblock ", t, flags=re.S)
    t = re.sub(r"https?://\S+", " url ", t)
    t = re.sub(r"[a-z]:[\\/]\S*|(?:\.{0,2}/)?(?:[\w.-]+/){2,}[\w.-]*", " path ", t)
    t = re.sub(r"\d+", " ", t)
    out = []
    for w in re.findall(r"[a-z][a-z_-]+", t):
        w = w.strip("-_")
        if len(w) < 3 or w in STOPWORDS:
            continue
        out.append(stem(w))
        if len(out) >= cap:
            break
    return out


CORRECTION_PATTERNS = [
    (r"^(non|nan|no|nope)\b", "« non » / « no »"),
    (r"\b(toujours|still) (pas|not|le meme|la meme|the same|broken|failing)\b", "« toujours pas » / « still not »"),
    (r"\b(ca|cela|it|this|that) (ne )?(marche|fonctionne|works?)( toujours| still)? ?(pas|plus)\b"
     r"|\b(doesn'?t|does not|didn'?t|isn'?t|not) work", "« ça ne marche pas » / « doesn't work »"),
    (r"\bn'?(a|ont) (pas|rien) (marche|fonctionne|change|corrige)", "« ça n'a pas marché »"),
    (r"\b(c'?est )?pas (ca|ce que|comme ca)\b|\bnot what i\b|\bthat'?s not\b", "« pas ça » / « not what I asked »"),
    (r"\bje (t'?ai|vous ai) (dit|demande)\b|\bi (said|asked|told you)\b", "« je t'ai dit » / « I said »"),
    (r"\b(tu as|t'?as|vous avez) (oublie|casse|supprime|ignore|mal)\b|\byou (forgot|broke|deleted|ignored|missed)\b",
     "« tu as oublié/cassé » / « you forgot/broke »"),
    (r"\b(refai[st]|recommence|reessaie|essaie encore|try again|redo|retry)\b", "« refais » / « try again »"),
    (r"\b(annule|revert|undo|reviens? en arriere|roll ?back)\b", "« annule » / « revert »"),
    (r"\b(c'?est|it'?s|that'?s) (faux|incorrect|wrong|pas bon|pas correct)\b|\bwrong\b", "« c'est faux » / « wrong »"),
    (r"\b(plutot|au lieu de|instead)\b", "« plutôt » / « instead »"),
    (r"^(arrete|stop|stoppe)\b", "« arrête » / « stop »"),
    (r"\bpourquoi (tu|as-tu|est-ce que tu)\b|\bwhy (did|do|are) you\b", "« pourquoi tu… » / « why did you »"),
    (r"\b(encore|again) (une |la meme |le meme |an |the same )?(erreur|error|bug|probleme|issue)\b",
     "« encore une erreur » / « error again »"),
]
CORRECTION_RES = [(re.compile(p), label) for p, label in CORRECTION_PATTERNS]


def correction_label(text):
    head = normalize(text)[:200]
    for rx, label in CORRECTION_RES:
        if rx.search(head):
            return label
    return None


NO_LABELS = {CORRECTION_PATTERNS[0][1], "« arrête » / « stop »"}


def declines_offer(text, prev_reply):
    """A bare "non" / "non, stop" answering a question of the assistant ("je l'ajoute ?") refuses an offer:
    the assistant did nothing wrong yet, so it is not a correction."""
    if not (prev_reply or "").strip().rstrip("*_` \n").endswith("?"):
        return False
    head = normalize(text)[:200]
    labels = {label for rx, label in CORRECTION_RES if rx.search(head)}
    return bool(labels) and labels <= NO_LABELS


# --------------------------------------------------------------------------- guardrails & defect reports
#
# Guardrails: the user pre-empts the assistant ("sans coder", "ne fais pas le build", "attends mon go").
# Each one is a rule the assistant should already know. Defect reports: after a delivery the user finds
# the problem ("l'os dépasse", "ça ne s'affiche pas"), i.e. the assistant did not check its own work.
# Matching tolerates typos (swapped, missing or extra letters: "sans cdore" = "sans coder").

GUARDRAIL_PHRASES = {
    "ne rien coder / modifier": [
        "sans coder", "sans code", "sans rien coder", "sans developper", "sans implementer", "sans modifier",
        "sans rien modifier", "sans toucher", "sans rien toucher", "ne code rien", "ne code pas", "ne codes pas",
        "pas de code", "pas coder", "dev rien", "ne dev rien", "pas de dev", "ne developpe pas", "ne developpe rien",
        "ne modifie rien", "ne modifie pas", "ne touche a rien", "ne touche pas", "touche a rien", "pas de modif",
        "pas de modification", "aucune modif", "aucune modification", "n implemente pas", "n implemente rien",
        "ne change rien", "ne change pas", "don t code", "do not code", "no code",
        "don t change", "do not change", "don t touch", "do not touch", "don t modify", "do not modify",
        "without coding", "don t implement", "do not implement", "no changes",
    ],
    "juste une question / réflexion": [
        "juste une question", "simple question", "question rapide", "juste pour savoir", "etait juste pour savoir",
        "seulement en brainstorming", "que du brainstorming", "juste du brainstorming", "brainstorm seulement",
        "brainstorming seulement", "juste reflechir", "reflechis seulement", "reflexion seulement",
        "analyse seulement", "juste analyser", "juste ton avis", "just asking", "just a question", "only brainstorm",
    ],
    "attendre une validation": [
        "avant de faire quoi que ce soit", "avant de coder", "avant de commencer", "avant de toucher",
        "avant de modifier", "attends mon go", "attend mon go", "attends mon feu vert", "attends mon accord",
        "attends ma validation", "propose avant", "propose moi avant", "propose moi ton resultat avant",
        "montre moi avant", "montre avant", "demande moi avant", "demande avant", "valide avec moi", "valider avec moi",
        "on valide avant", "ne commence pas", "pas tout de suite", "pas de suite", "plan seulement", "juste le plan",
        "wait for my go", "ask before", "ask me before", "before doing anything",
        "check with me", "plan only",
    ],
    "pas de build / commit / push / déploiement": [
        "ne fais pas le build", "ne fait pas le build", "ne build pas", "pas de build", "sans build", "sans le build",
        "ne lance pas le build", "ne commit pas", "ne commite pas", "pas de commit", "sans commit", "sans commiter",
        "ne push pas", "ne pousse pas", "pas de push", "sans push", "sans pousser", "ne merge pas", "pas de merge",
        "sans merge", "ne fusionne pas", "ne deploie pas", "pas de deploiement", "pas de mep", "sans deployer",
        "don t commit", "do not commit", "don t push", "do not push", "don t build", "do not build",
        "don t deploy", "do not deploy", "don t merge", "do not merge",
    ],
    "ne rien supprimer / casser": [
        "ne supprime rien", "ne supprime pas", "sans supprimer", "ne casse rien", "ne casse pas", "sans rien casser",
        "fais attention", "attention a ne pas", "garde l existant", "ne perds rien", "don t delete", "do not delete",
        "don t remove", "do not remove", "be careful",
    ],
    "périmètre limité": [
        "rien d autre", "rien de plus", "ne change que", "ne touche qu a", "ne modifie que", "que ce fichier",
        "only this", "nothing else", "only change",
    ],
}
DEFECT_PHRASES = {
    "défaut signalé": [
        "il y a un probleme", "y a un probleme", "il y a un souci", "y a un souci", "il y a un bug", "y a un bug",
        "y a un pb", "il y a un pb", "y a pb", "c est bugge", "ca bugge", "s affiche pas", "s affiche plus",
        "apparait pas", "apparait plus", "charge pas", "compile pas", "plus rien ne",
        "c est casse", "c est ko", "est ko", "ca plante", "ca crash", "ca bug", "ca beug", "ne s affiche pas",
        "ne s affiche plus", "s affiche mal", "n apparait pas", "n apparait plus", "a disparu", "ont disparu",
        "depasse", "depassent", "deborde", "debordent", "decale", "decalee", "decales", "decalees", "pas aligne",
        "mal aligne", "pas alignes", "chevauche", "se chevauchent", "se superpose", "se superposent", "erreur 500",
        "erreur 404", "page blanche", "ecran blanc", "ne charge pas", "ne repond pas", "ne compile pas",
        "ne compile plus", "tests echouent", "test echoue", "regression", "it s broken", "is broken",
        "doesn t show", "not showing", "is missing", "crashes", "error 500", "blank page", "not loading",
    ],
}


def osa_distance(a, b):
    """Edit distance counting a swap of two adjacent letters as one edit."""
    d = [[i + j if i * j == 0 else 0 for j in range(len(b) + 1)] for i in range(len(a) + 1)]
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (a[i - 1] != b[j - 1]))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[len(a)][len(b)]


@functools.lru_cache(maxsize=None)
def _letters(word):
    return "".join(sorted(word))


def typo_equal(word, target):
    """'cdore' ~ 'coder', 'buld' ~ 'build', 'commti' ~ 'commit'. Short words must match exactly."""
    if word == target:
        return True
    if len(target) <= 3 or len(word) <= 3 or abs(len(word) - len(target)) > 1:
        return False
    if len(word) == len(target) and _letters(word) == _letters(target):
        return True  # letters typed in the wrong order ("cdore")
    if word[0] != target[0] or osa_distance(word, target) > 1:
        return False
    # One wrong letter in a short word makes another real word ("change" / "charge"): only accept a
    # missing or extra letter there.
    return len(target) > 6 or len(word) != len(target)


def normalize_with_map(text):
    """Lowercase, accent-free text plus, for each of its characters, the index in the original text."""
    out, index = [], []
    for i, ch in enumerate((text or "").replace("’", "'").replace("‘", "'")):
        for c in unicodedata.normalize("NFKD", ch):
            if not unicodedata.combining(c):
                out.append(c.lower())
                index.append(i)
    return "".join(out), index


class PhraseMatcher:
    """Finds the phrases of several categories in prompts, tolerating typos and one extra word between words."""

    def __init__(self, categories):
        self.phrases = []
        for cat, phrases in categories.items():
            for p in phrases:
                toks = re.findall(r"[a-z0-9]+", normalize(p))
                if toks:
                    self.phrases.append((cat, p, toks))
        self.targets = {t for _c, _p, toks in self.phrases for t in toks}
        self.known = {}  # target word -> set of vocabulary words that match it

    def _prepare(self, words):
        new = [w for w in words if w not in self.seen] if hasattr(self, "seen") else list(words)
        self.seen = getattr(self, "seen", set()) | set(new)
        for t in self.targets:
            bucket = self.known.setdefault(t, set())
            bucket.update(w for w in new if typo_equal(w, t))

    def find(self, text, limit_tokens=400):
        """[(category, phrase, start offset in the original text)]"""
        norm, index = normalize_with_map(text)
        spans = [(m.group(0), m.start()) for m in re.finditer(r"[a-z0-9]+", norm)][:limit_tokens]
        words = [w for w, _ in spans]
        self._prepare(set(words))
        found, done = [], set()
        for cat, phrase, toks in self.phrases:
            if cat in done:
                continue
            first = self.known[toks[0]]
            for i, w in enumerate(words):
                if w not in first:
                    continue
                if any(words[k] in NEGATORS for k in range(max(0, i - 2), i)):
                    continue  # "oublie pas de push" asks for the opposite
                j, ok = i, True
                for t in toks[1:]:
                    if j + 1 < len(words) and words[j + 1] in self.known[t]:
                        j += 1
                    elif j + 2 < len(words) and words[j + 2] in self.known[t]:
                        j += 2  # one extra word allowed: "ne code strictement rien"
                    else:
                        ok = False
                        break
                if ok:
                    found.append((cat, phrase, index[spans[i][1]] if spans else 0))
                    done.add(cat)
                    break
        return found


NEGATORS = {"oublie", "oublies", "oublier", "oubliez", "forget"}
GUARDRAIL_MATCHER = PhraseMatcher(GUARDRAIL_PHRASES)
DEFECT_MATCHER = PhraseMatcher(DEFECT_PHRASES)


def weighted_jaccard(a, b, idf):
    if not a or not b:
        return 0.0
    inter = sum(idf.get(w, 1.0) for w in a & b)
    if not inter:
        return 0.0
    return inter / sum(idf.get(w, 1.0) for w in a | b)


# --------------------------------------------------------------------------- analysis

def compute_idf(sessions):
    df = Counter()
    n = 0
    for s in sessions:
        for t in s.turns:
            if t.kind == "prompt":
                t.tokens = tokenize(t.text)
                df.update(set(t.tokens))
                n += 1
    return {w: math.log((n + 1.0) / (c + 0.5)) for w, c in df.items()}, df, n


def session_metrics(s, idf):
    corrections = rephrasings = guardrails = defects = declined = 0
    labels = Counter()
    prev = None
    for i, t in enumerate(s.turns):
        t.flags = []
        t.guardrails = GUARDRAIL_MATCHER.find(t.text) if t.kind == "prompt" else []
        t.defects = DEFECT_MATCHER.find(t.text) if t.kind == "prompt" and i > 0 else []
        if t.guardrails:
            guardrails += 1
            t.flags.append("GARDE_FOU")
        if t.defects:
            defects += 1
            t.flags.append("DEFAUT_SIGNALE")
        if t.kind == "prompt" and i > 0:
            label = correction_label(t.text)
            if label and declines_offer(t.text, s.turns[i - 1].reply):
                declined += 1
                t.flags.append("REFUS_PROPOSITION")
            elif label:
                corrections += 1
                labels[label] += 1
                t.flags.append("CORRECTION")
            if prev is not None and len(set(t.tokens)) >= 3 and len(set(prev.tokens)) >= 3 \
                    and weighted_jaccard(set(t.tokens), set(prev.tokens), idf) >= 0.5:
                rephrasings += 1
                t.flags.append("REFORMULATION")
        if t.interrupted:
            t.flags.append("INTERRUPTION")
        if t.rejections:
            t.flags.append("REFUS_OUTIL")
        if t.downvote:
            t.flags.append("VOTE_NEGATIF")
        if t.failed:
            t.flags.append("ECHEC_REPONSE")
        if t.kind == "prompt":
            prev = t
    interrupts = sum(1 for t in s.turns if t.interrupted)
    rejections = sum(t.rejections for t in s.turns)
    tool_errors = sum(t.tool_errors for t in s.turns)
    downvotes = sum(1 for t in s.turns if t.downvote)
    failed = sum(1 for t in s.turns if t.failed)
    friction = (2 * corrections + 2 * rephrasings + 2 * interrupts + 1.5 * downvotes + 1.5 * defects
                + guardrails + rejections + failed + 0.5 * min(tool_errors, 10))
    s.metrics = {
        "rounds": len(s.turns),
        "prompts": len(s.prompts()),
        "corrections": corrections,
        "declined_offers": declined,
        "rephrasings": rephrasings,
        "guardrails": guardrails,
        "defects": defects,
        "interruptions": interrupts,
        "tool_rejections": rejections,
        "tool_errors": tool_errors,
        "downvotes": downvotes,
        "failed_responses": failed,
        "tool_calls": sum(sum(t.tools.values()) for t in s.turns),
        "friction": round(friction, 1),
    }
    return labels


class UnionFind:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def cluster_prompts(sessions, idf, df, n_prompts, threshold, min_sessions, max_clusters, excerpt_chars):
    items = []
    for s in sessions:
        for i, t in enumerate(s.turns):
            if t.kind == "prompt" and "CORRECTION" not in t.flags and len(set(t.tokens)) >= 3:
                items.append((s, i, t, set(t.tokens)))
    if not items:
        return []
    # Candidates come from each prompt's rarest words: a fixed df cut-off would drop the very words that make
    # a task recognisable ("junit", "mockito") as soon as they recur in more than a few prompts.
    postings = defaultdict(list)
    uf = UnionFind(len(items))
    for j, (s, i, t, toks) in enumerate(items):
        cand = Counter()
        for w in sorted(toks, key=lambda x: (df.get(x, 0), x))[:6]:
            for k in postings[w]:
                cand[k] += 1
        for k, _ in cand.most_common(300):
            s2, i2, _t2, toks2 = items[k]
            if s2 is s and abs(i2 - i) <= 1:
                continue  # adjacent prompts in one session = rephrasing, not a recurring task
            if weighted_jaccard(toks, toks2, idf) >= threshold:
                uf.union(k, j)
        for w in toks:
            postings[w].append(j)

    groups = defaultdict(list)
    for j in range(len(items)):
        groups[uf.find(j)].append(items[j])

    surface = defaultdict(Counter)  # stem -> original word forms, for readable labels
    clusters = []
    for members in groups.values():
        sess = {id(m[0]): m[0] for m in members}
        if len(members) < 2 or (len(sess) < min_sessions and len(members) < 3):
            continue
        term_count = Counter()
        for _s, _i, t, toks in members:
            term_count.update(toks)
            for w in re.findall(r"[a-z][a-z_-]+", normalize(t.text)):
                surface[stem(w.strip("-_"))][w.strip("-_")] += 1
        top_terms = [surface[w].most_common(1)[0][0] if surface[w] else w
                     for w, c in sorted(term_count.items(),
                                        key=lambda kv: (-round(kv[1] * idf.get(kv[0], 1.0), 2), -len(kv[0]), kv[0]))[:6]]
        days = {fmt_day(t.ts or s.start) for s, _i, t, _ in members}
        sl = list(sess.values())
        avg_rounds = sum(x.metrics["rounds"] for x in sl) / len(sl)
        avg_friction = sum(x.metrics["friction"] for x in sl) / len(sl)
        stamps = [t.ts or s.start for s, _i, t, _ in members if (t.ts or s.start)]
        examples, used = [], set()
        for s, i, t, _ in sorted(members, key=lambda m: (m[2].ts or m[0].start or NOW), reverse=True):
            if id(s) in used and len(examples) < len(sess):
                continue
            used.add(id(s))
            examples.append({"session": s.id[:8], "source": s.source, "project": s.project,
                             "date": fmt_day(t.ts or s.start), "turn": i + 1,
                             "text": excerpt(t.text, excerpt_chars)})
            if len(examples) >= 4:
                break
        clusters.append({
            "terms": top_terms,
            "prompts": len(members),
            "sessions": len(sess),
            "days": len(days),
            "openers": sum(1 for _s, i, _t, _ in members if i == 0),
            "projects": sorted({x.project or "?" for x in sl}),
            "sources": sorted({x.source for x in sl}),
            "first_seen": fmt_day(min(stamps)) if stamps else "?",
            "last_seen": fmt_day(max(stamps)) if stamps else "?",
            "avg_session_rounds": round(avg_rounds, 1),
            "avg_session_friction": round(avg_friction, 1),
            "examples": examples,
            "score": round(2 * len(sess) + len(days) + 0.5 * len(members) + 0.3 * avg_friction, 1),
        })
    clusters.sort(key=lambda c: -c["score"])
    return clusters[:max_clusters]


NOISE_COMMANDS = set("""
ls dir cd pushd popd pwd cat type head tail less more echo printf grep rg find fd sed awk wc sort uniq
cut tr file od xxd hexdump stat du df mkdir rmdir rm del cp copy mv move touch sleep true false which where whoami date
clear env set export unset source exit read test chmod chown xargs tee jq base64
for foreach while until if then else elif fi do done function case esac try catch return
get-childitem get-content set-content add-content select-string select-object where-object
foreach-object sort-object measure-object format-table format-list test-path get-item get-itemproperty
get-location set-location write-host write-output start-sleep out-file out-null new-item remove-item
copy-item move-item get-process stop-process get-command get-date get-nettcpconnection join-path
split-path resolve-path convertto-json convertfrom-json
""".split())
NOISE_GIT_SUBCOMMANDS = {"status", "diff", "log", "show", "branch", "rev-parse", "remote", "config", "ls-files"}
INTERPRETERS = {"python", "python3", "py", "node", "perl", "ruby", "bash", "sh", "pwsh", "powershell", "deno", "bun"}
TEMP_PATH_RE = re.compile(r"(?i)scratchpad|[\\/]te?mp[\\/]|appdata[\\/]local[\\/]temp")


def _segment_key(seg):
    words = seg.split()
    while words and (re.match(r"^[A-Za-z_]\w*=", words[0]) or words[0] in ("&", ".", "do", "then", "else",
                                                                          "time", "sudo", "exec", "!")):
        words = words[1:]
    if not words:
        return None
    first = words[0].strip("\"'()")
    if re.match(r"^\$\w*", first):
        return None
    if TEMP_PATH_RE.search(first):
        return None
    prog = os.path.basename(first.replace("\\", "/")).lower()
    if re.search(r"\.(py|sh|ps1|js|mjs|ts|cmd|bat)$", prog):
        return prog
    if prog in NOISE_COMMANDS or len(prog) < 2 or not re.match(r"^[a-z][\w.+-]*$", prog):
        return None
    rest = words[1:]
    if prog in INTERPRETERS:
        if len(rest) >= 2 and rest[0] == "-m":
            return "%s -m %s" % (prog, rest[1])
        script = next((w for w in rest if not w.startswith("-")), None)
        if rest and rest[0].startswith("-") or not script or TEMP_PATH_RE.search(script):
            return None  # inline code (python -, node -e, perl -pi …) or a throwaway script
        return os.path.basename(script.strip("\"'").replace("\\", "/")).lower()
    if prog == "git":
        while rest and rest[0].startswith("-"):
            rest = rest[2:] if rest[0] in ("-C", "-c") else rest[1:]
        if not rest or rest[0] in NOISE_GIT_SUBCOMMANDS:
            return None
        return "git " + rest[0]
    key = [prog]
    for w in rest:
        if w.startswith("-"):
            continue  # `mvn -q test` -> `mvn test`
        if len(key) < 3 and re.match(r"^[a-z][a-z0-9:_-]*$", w):
            key.append(w)
        else:
            break
    return " ".join(key)


QUOTED_RE = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"", re.S)


def _neutralize_quote(m):
    inner = m.group(0)[1:-1]
    # Keep quoted paths (`& "C:\a b\run.ps1"`), drop quoted code / patterns (`node -e '…'`, `grep "a|b"`).
    if re.search(r"[\\/]", inner) and not re.search(r"[\n|;&<>$`]", inner):
        return inner.replace(" ", "_")
    return "Q"


def command_keys(cmd):
    """Meaningful sub-commands of a shell invocation, with the segment they came from.
    'cd x && git add -A; git commit -m "…"' -> [("git add", …), ("git commit", …)]"""
    keys = []
    for line in QUOTED_RE.sub(_neutralize_quote, cmd or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for seg in re.split(r"\s*(?:&&|\|\||;|\|)\s*", line):
            seg = seg.strip()
            k = _segment_key(seg)
            if k and k not in (x for x, _ in keys):
                keys.append((k, seg))
        if "<<" in line:
            break  # the rest is a heredoc body, not commands
    return keys


def recurring_commands(sessions, excerpt_chars, min_sessions=3, limit=15):
    by_key = defaultdict(lambda: {"sessions": set(), "count": 0, "example": None})
    for s in sessions:
        for t in s.turns:
            for c in t.shell:
                for k, seg in command_keys(c):
                    e = by_key[k]
                    e["sessions"].add(s.id)
                    e["count"] += 1
                    e["example"] = e["example"] or excerpt(seg, excerpt_chars)
    rows = [{"command": k, "sessions": len(v["sessions"]), "count": v["count"], "example": v["example"]}
            for k, v in by_key.items() if len(v["sessions"]) >= min_sessions]
    rows.sort(key=lambda r: (-r["sessions"], -r["count"]))
    return rows[:limit]


# --------------------------------------------------------------------------- existing skills / instructions

def read_frontmatter(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = f.read(4000)
    except OSError:
        return None, None
    m = re.match(r"^---\s*\n(.*?)\n---", head, re.S)
    if not m:
        return None, None
    fm = m.group(1)
    name = re.search(r"^name:\s*(.+)$", fm, re.M)
    desc = re.search(r"^description:\s*(.+)$", fm, re.M)
    return (name.group(1).strip().strip("'\"") if name else None,
            desc.group(1).strip().strip("'\"") if desc else None)


def skill_dirs(sources, claude_home, copilot_home, cwd):
    dirs = []
    if "claude" in sources:
        dirs += [os.path.join(claude_home, "skills"), os.path.join(cwd, ".claude", "skills")]
    if sources & COPILOT_SOURCES:
        dirs += [os.path.join(copilot_home, "skills"), os.path.join(HOME, ".claude", "skills"),
                 os.path.join(HOME, ".agents", "skills"), os.path.join(cwd, ".github", "skills"),
                 os.path.join(cwd, ".claude", "skills"), os.path.join(cwd, ".agents", "skills")]
    seen, out = set(), []
    for d in dirs:
        d = os.path.normpath(d)
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def installed_plugin_paths(claude_home):
    try:
        with open(os.path.join(claude_home, "plugins", "installed_plugins.json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    out = []
    for key, entries in (data.get("plugins") or {}).items():
        for e in entries if isinstance(entries, list) else [entries]:
            if isinstance(e, dict) and e.get("installPath"):
                out.append((key.split("@")[0], e["installPath"]))
    return out


def existing_skills(sources, claude_home, copilot_home, cwd, project_dirs=()):
    found = {}
    dirs = skill_dirs(sources, claude_home, copilot_home, cwd)
    for pd in project_dirs:
        if "claude" in sources:
            dirs.append(os.path.join(pd, ".claude", "skills"))
        if sources & COPILOT_SOURCES:
            dirs += [os.path.join(pd, ".github", "skills"), os.path.join(pd, ".agents", "skills")]
    for d in dirs:
        for p in glob.glob(os.path.join(d, "*", "SKILL.md")):
            name, desc = read_frontmatter(p)
            name = name or os.path.basename(os.path.dirname(p))
            found.setdefault(name, {"name": name, "location": os.path.dirname(os.path.dirname(p)),
                                    "description": excerpt(desc or "", 140)})
    if "claude" in sources:
        for plugin, path in installed_plugin_paths(claude_home):
            for p in glob.glob(os.path.join(path, "skills", "*", "SKILL.md")):
                name, desc = read_frontmatter(p)
                name = "%s:%s" % (plugin, name or os.path.basename(os.path.dirname(p)))
                found.setdefault(name, {"name": name, "location": "plugin " + plugin,
                                        "description": excerpt(desc or "", 140)})
    return sorted(found.values(), key=lambda x: x["name"])


def existing_commands(sources, claude_home, cwd, project_dirs=()):
    """Slash commands defined as files: Claude `.claude/commands/*.md`, Copilot prompt files `.github/prompts/*.prompt.md`."""
    roots = [cwd] + list(project_dirs)
    patterns = []
    if "claude" in sources:
        patterns += [os.path.join(claude_home, "commands", "*.md")]
        patterns += [os.path.join(r, ".claude", "commands", "*.md") for r in roots]
    if sources & COPILOT_SOURCES:
        patterns += [os.path.join(r, ".github", "prompts", "*.prompt.md") for r in roots]
    found = {}
    for pattern in patterns:
        for p in glob.glob(pattern):
            name = "/" + re.sub(r"(\.prompt)?\.md$", "", os.path.basename(p))
            found.setdefault(name, {"name": name, "location": os.path.dirname(p)})
    return sorted(found.values(), key=lambda x: x["name"])


def instruction_files(sources, claude_home, cwd, project_dirs=()):
    roots = [cwd] + list(project_dirs)
    candidates = []
    if "claude" in sources:
        candidates.append(os.path.join(claude_home, "CLAUDE.md"))
        for r in roots:
            candidates += [os.path.join(r, "CLAUDE.md"), os.path.join(r, ".claude", "CLAUDE.md"),
                           os.path.join(r, "CLAUDE.local.md")]
            candidates += glob.glob(os.path.join(r, ".claude", "agents", "*.md"))
    if sources & COPILOT_SOURCES:
        for r in roots:
            candidates += [os.path.join(r, ".github", "copilot-instructions.md"), os.path.join(r, "AGENTS.md")]
            for pattern in ("instructions/*.instructions.md", "agents/*.agent.md"):
                candidates += glob.glob(os.path.join(r, ".github", pattern))
    out = []
    for p in candidates:
        p = os.path.normpath(p)
        if os.path.isfile(p) and p not in out:
            out.append(p)
    return out


# --------------------------------------------------------------------------- orchestration

SOURCE_ALIASES = {
    "claude": {"claude"},
    "copilot": {"copilot-cli", "vscode", "jetbrains"},
    "copilot-cli": {"copilot-cli"},
    "vscode": {"vscode"},
    "jetbrains": {"jetbrains"},
    "all": {"claude", "copilot-cli", "vscode", "jetbrains"},
}


def load_sessions(sources, args):
    out = []
    if "claude" in sources:
        out += list(load_claude(args.claude_home))
    if sources & COPILOT_SOURCES:
        # The Copilot CLI store also holds IntelliJ and VS Code agent sessions: keep those of the requested tools.
        out += [s for s in load_copilot_cli(args.copilot_home) if s.source in sources]
    if "vscode" in sources:
        out += list(load_vscode(args.vscode_user_dir))
    if "jetbrains" in sources:
        out += load_jetbrains(args.jetbrains_dir)
    return drop_copied_turns(out)


def drop_copied_turns(sessions):
    """Resumed / forked sessions start with a verbatim copy of the parent's history (same timestamps).
    Keep each (timestamp, text) once, in the longest session, so copies do not look like repetition."""
    seen = set()
    for s in sorted(sessions, key=lambda x: -len(x.turns)):
        kept = []
        for t in s.turns:
            key = (t.ts.isoformat() if t.ts else None, t.text[:300])
            if key[0] is not None and key in seen:
                continue
            seen.add(key)
            kept.append(t)
        s.turns = kept
    return sessions


def unknown_project(s):
    """Sessions the store does not tie to a folder (VS Code empty window, old JetBrains stores)."""
    return not s.cwd and (not s.project or s.project == "(sans dossier)" or s.project.startswith("jetbrains:"))


def select_sessions(sessions, args):
    """Sessions matching the filters. With --project, sessions whose project is unknown cannot be matched:
    they are set aside in args.unscoped so the digest can list them instead of silently dropping them."""
    cutoff = NOW - dt.timedelta(days=args.days) if args.days else None
    proj = (args.project or "").lower()
    excluded = [e.lower() for e in args.exclude_session or []]
    excluded_projects = [p.lower() for p in getattr(args, "exclude_project", None) or []]
    kept, unscoped = [], []
    for s in sessions:
        if not s.turns:
            continue
        if cutoff and (s.end or s.start) and (s.end or s.start) < cutoff:
            continue
        if any(s.id.lower().startswith(e) for e in excluded):
            continue
        where = ((s.project or "") + " " + (s.cwd or "")).lower()
        if any(p in where for p in excluded_projects):
            continue
        if proj and proj not in (s.project or "").lower() and proj not in (s.cwd or "").lower():
            if unknown_project(s):
                unscoped.append(s)
            continue
        kept.append(s)
    args.unscoped = unscoped
    return kept


# --------------------------------------------------------------------------- evidence that spares opening sessions
#
# Reading whole sessions is what makes an audit expensive (a long session costs 2 000 to 8 000 tokens).
# The sections below put the context the agent would go looking for straight into the digest.

EPISODE_WEIGHTS = {"CORRECTION": 3, "DEFAUT_SIGNALE": 2.5, "REFORMULATION": 2, "VOTE_NEGATIF": 2,
                   "GARDE_FOU": 1.5, "INTERRUPTION": 1.5, "REFUS_OUTIL": 1, "ECHEC_REPONSE": 1}
# The flagged turn reacts to the previous one.
REACTION_FLAGS = {"CORRECTION", "REFORMULATION", "DEFAUT_SIGNALE", "GARDE_FOU"}
# "oups, je voulais pas arrêter, continue": the stop was a slip, not a sign the assistant went wrong.
ACCIDENTAL_RE = re.compile(r"^(pardon|desole|oups|mince|zut|sorry|oops)?[ ,!.]*(je voulais pas|j'ai coupe|"
                           r"re-?continue|continue|reprend|reprends|vas-y|go on|keep going|carry on)\b")


def tail_excerpt(text, limit):
    """End of an assistant reply: where questions and conclusions are."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) > limit:
        text = "…" + text[-(limit - 1):].split(" ", 1)[-1]
    return redact(text)


def tools_summary(turn, top=3):
    families = Counter()
    for k, v in turn.tools.items():
        families[tool_family(k)] += v
    parts = ["%s×%d" % kv for kv in families.most_common(top)]
    if turn.tool_errors:
        parts.append("%d erreur(s)" % turn.tool_errors)
    if turn.rejections:
        parts.append("%d refus" % turn.rejections)
    return ", ".join(parts) or "aucun outil"


def friction_episodes(sessions, limit=12, chars=140):
    """Friction moments with their context: what was asked, how the reply ended, how the user reacted."""
    episodes = []
    for s in sessions:
        found = []
        for i, t in enumerate(s.turns):
            kinds = [f for f in t.flags if f in EPISODE_WEIGHTS]
            if not kinds:
                continue
            if set(kinds) & REACTION_FLAGS and i > 0:
                before, reaction = s.turns[i - 1], t
            else:
                before, reaction = t, (s.turns[i + 1] if i + 1 < len(s.turns) else None)
            if not set(kinds) & REACTION_FLAGS and reaction is not None and len(reaction.text) <= 60 \
                    and ACCIDENTAL_RE.search(normalize(reaction.text)):
                continue  # counted in the session metrics, but tells nothing about the cause
            # A rejected tool call also stops the turn: count the pair once.
            weights = [EPISODE_WEIGHTS[k] for k in kinds if not (k == "INTERRUPTION" and "REFUS_OUTIL" in kinds)]
            found.append({
                "score": sum(weights) + 0.05 * s.metrics.get("friction", 0),
                "kinds": kinds, "session": s.id[:8], "source": s.source, "project": s.project,
                "date": fmt_day(t.ts or s.start), "turn": i + 1,
                "asked": excerpt(before.text, chars), "reply": tail_excerpt(before.reply, chars),
                "tools": tools_summary(before),
                "reaction": excerpt(reaction.text, chars) if reaction is not None else "",
            })
        found.sort(key=lambda e: -e["score"])
        episodes += found[:2]  # at most two per session, so one long session does not fill the list
    episodes.sort(key=lambda e: -e["score"])
    for e in episodes:
        e.pop("score")
    return episodes[:limit]


def around(text, offset, chars):
    """Excerpt of the original text around a match."""
    start = max(0, offset - chars // 3)
    return ("…" if start else "") + excerpt(text[start:], chars)


def guardrail_report(sessions, examples=3, chars=130):
    """Pre-emptive instructions typed by the user, by category."""
    cats = {}
    for s in sessions:
        for i, t in enumerate(s.turns):
            for cat, phrase, offset in t.guardrails:
                e = cats.setdefault(cat, {"count": 0, "sessions": set(), "examples": []})
                e["count"] += 1
                e["sessions"].add(s.id)
                if len(e["examples"]) < examples:
                    e["examples"].append({"date": fmt_day(t.ts or s.start), "session": s.id[:8], "project": s.project,
                                          "turn": i + 1, "text": around(t.text, offset, chars)})
    for e in cats.values():
        e["sessions"] = len(e["sessions"])
    return dict(sorted(cats.items(), key=lambda kv: -kv[1]["count"]))


def defect_report(sessions, examples=6, chars=130):
    """Problems the user found after a delivery, with what had been delivered."""
    total, sess, ex = 0, set(), []
    for s in sessions:
        for i, t in enumerate(s.turns):
            if not t.defects:
                continue
            total += 1
            sess.add(s.id)
            if len(ex) < examples * 3:
                prev = s.turns[i - 1] if i > 0 else None
                ex.append({"date": fmt_day(t.ts or s.start), "session": s.id[:8], "project": s.project, "turn": i + 1,
                           "text": around(t.text, t.defects[0][2], chars),
                           "after": excerpt(prev.text, 90) if prev else "",
                           "checked": bool(prev and prev.tools)})
    ex.sort(key=lambda e: e["date"], reverse=True)
    seen, picked = set(), []
    for e in ex:  # one example per session first
        if e["session"] not in seen:
            seen.add(e["session"])
            picked.append(e)
    return {"count": total, "sessions": len(sess), "examples": picked[:examples]}


def tool_error_report(sessions, limit=8, chars=140):
    """Most frequent tool failures, grouped by tool and first line of the message."""
    groups = defaultdict(lambda: {"count": 0, "sessions": set(), "example": ""})
    for s in sessions:
        for t in s.turns:
            for tool, text, _cmd in t.error_samples:
                first = next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")
                sig = re.sub(r"[\w.-]*[\\/][^\s'\"]*", "<chemin>", normalize(first))
                sig = re.sub(r"\d+", "0", sig)[:70]
                g = groups[(tool_family(tool), sig)]
                g["count"] += 1
                g["sessions"].add(s.id)
                g["example"] = g["example"] or excerpt(first, chars)
    rows = [{"tool": k[0], "count": v["count"], "sessions": len(v["sessions"]), "example": v["example"]}
            for k, v in groups.items()]
    rows.sort(key=lambda r: (-r["count"], -r["sessions"]))
    return rows[:limit]


# Why a tool call failed, from the start of its error message (accent-free, lower case). The order matters:
# the first match wins. Most of these cost the user nothing visible, only time and tokens, so they never show
# up as corrections: this is where the assistant can improve without the user having to say anything.
ERROR_CAUSES = [
    ("dossier", "mauvais dossier ou pas un dépôt git",
     r"not a git repository|n'est pas un depot|cannot find path|could not find (a part of )?the path|"
     r"chemin d'acces (specifie )?introuvable|no such file or directory"),
    ("edition", "édition de fichier ratée par l'assistant",
     r"string to replace not found|old_string|file has not been read|has been modified since|must be read|"
     r"found \d+ matches|is not unique|file not read"),
    ("environnement", "commande, interpréteur ou module introuvable",
     r"command not found|n'est pas reconnu|is not recognized|introuvable|not found|enoent|no module named|"
     r"modulenotfounderror|cannot find module|is not installed|n'est pas installe"),
    ("syntaxe", "syntaxe du shell (guillemets, heredoc, bash / PowerShell)",
     r"syntax error|unexpected (eof|token|end of file)|unterminated|parsererror|missing terminator|here-document|"
     r"unexpected character|the string is missing|jeton inattendu|erreur de syntaxe"),
    ("delai", "délai dépassé", r"timed? ?out|timeout|deadline exceeded|delai (d'attente )?depasse"),
    ("acces", "permission ou accès refusé",
     r"permission denied|access (is )?denied|acces refuse|eacces|eperm|operation not permitted|unauthorized|forbidden"),
    ("tests", "tests ou build en échec",
     r"tests? run:|build failure|failures?:|failed tests?|npm err|error ts\d|compilation (failed|error)|"
     r"echec de la compilation|assertionerror|build failed"),
    ("muet", "échec sans message exploitable (code de sortie seul)", r"^$"),
]
ERROR_CAUSE_RES = [(key, label, re.compile(rx)) for key, label, rx in ERROR_CAUSES]


EXIT_LINE_RE = re.compile(r"(?i)^(error:? )?(exit code|exited with code|process exited with code|command failed:? exit code)"
                          r"\s*:?\s*-?\d+:?\s*")
NOISE_LINE_RE = re.compile(r"^<[\w-]+/?>$|^This result includes Artifact content")
ERROR_WORD_RE = re.compile(r"(?i)error|erreur|exception|failed|failure|echec|échec|introuvable|not found|cannot|can't|"
                           r"impossible|denied|refus|timed out|fatal|traceback|au caract[eè]re|unexpected|invalid")


def error_message(text):
    """The informative lines of an error: without the 'Exit code N' header and bare marker lines."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip() and not NOISE_LINE_RE.match(ln.strip())]
    if lines:
        lines[0] = EXIT_LINE_RE.sub("", lines[0]).strip()
    return [ln for ln in lines if ln]


def error_line(lines):
    """A shell error mixes stdout and stderr: show the first line that reads like an error, else the last one."""
    return next((ln for ln in lines if ERROR_WORD_RE.search(ln)), lines[-1] if lines else "")


def error_cause(tool, text):
    lines = error_message(text)
    if not lines:
        return "muet", ERROR_CAUSES[-1][1]
    joined = " ".join(" ".join(lines).split())
    head = normalize(error_line(lines) + " " + joined[:300] + " " + joined[-500:])
    for key, label, rx in ERROR_CAUSE_RES:
        if rx.search(head):
            return key, label
    if tool_family(tool).startswith("mcp:"):
        return "externe", "outil externe (serveur MCP, navigateur)"
    return "autre", "autre (message à lire)"


def tool_error_causes(sessions, examples=3, chars=120):
    """Tool failures grouped by cause, with the commands that fail most: the assistant's own avoidable cost."""
    causes = {}
    for s in sessions:
        for t in s.turns:
            for tool, text, cmd in t.error_samples:
                key, label = error_cause(tool, text)
                c = causes.setdefault(key, {"label": label, "count": 0, "sessions": set(), "tools": Counter(),
                                            "commands": Counter(), "examples": []})
                c["count"] += 1
                c["sessions"].add(s.id)
                c["tools"][tool_family(tool)] += 1
                keys = command_keys(cmd) if cmd else []
                if keys:
                    c["commands"][keys[-1][0]] += 1
                if len(c["examples"]) < examples:
                    first = error_line(error_message(text))
                    c["examples"].append({"date": fmt_day(t.ts or s.start), "session": s.id[:8], "tool": tool_family(tool),
                                          "command": excerpt(cmd, 90) if cmd else "", "error": excerpt(first, chars)})
    rows = []
    for key, c in causes.items():
        rows.append({"cause": key, "label": c["label"], "count": c["count"], "sessions": len(c["sessions"]),
                     "tools": c["tools"].most_common(3), "commands": c["commands"].most_common(4),
                     "examples": c["examples"]})
    rows.sort(key=lambda r: -r["count"])
    return rows


ACTIVE_GAP = dt.timedelta(minutes=20)  # longer silence = the session was idle


def active_intervals(s):
    """Periods during which a session was being worked on: turn timestamps merged when less than 20 min apart."""
    stamps = sorted({t.ts for t in s.turns if t.ts} | ({s.end} if s.end else set()))
    out = []
    for ts in stamps:
        if out and ts - out[-1][1] <= ACTIVE_GAP:
            out[-1][1] = ts
        else:
            out.append([ts, ts])
    return [(a, b + dt.timedelta(minutes=2)) for a, b in out]


def same_workspace(a, b):
    if a.project and a.project == b.project:
        return True
    ca, cb = os.path.normcase(a.cwd or ""), os.path.normcase(b.cwd or "")
    if not ca or not cb:
        return False
    return ca == cb or ca.startswith(cb.rstrip("\\/") + os.sep) or cb.startswith(ca.rstrip("\\/") + os.sep)


def overlap_report(sessions, examples=5):
    """Sessions active at the same time on the same project, and changes one session attributes to another.
    Parallel sessions writing to the same folders see each other's uncommitted work without knowing whose it is."""
    timed = [(s, active_intervals(s)) for s in sessions if s.client != "jetbrains-nitrite" and s.start]
    timed = [(s, iv) for s, iv in timed if iv]
    pairs, involved, days, per_project = [], set(), set(), Counter()
    for i in range(len(timed)):
        a, ia = timed[i]
        for j in range(i + 1, len(timed)):
            b, ib = timed[j]
            if ia[-1][1] < ib[0][0] or ib[-1][1] < ia[0][0] or not same_workspace(a, b):
                continue
            minutes, first = 0.0, None
            for x0, x1 in ia:
                for y0, y1 in ib:
                    lo, hi = max(x0, y0), min(x1, y1)
                    if hi > lo:
                        minutes += (hi - lo).total_seconds() / 60
                        first = first or lo
            if minutes >= 2:
                pairs.append({"date": fmt_day(first), "project": a.project, "sessions": [a.id[:8], b.id[:8]],
                              "minutes": int(minutes)})
                involved |= {a.id, b.id}
                days.add(fmt_day(first))
                per_project[a.project or "?"] += 1
    # Peak: most sessions active at once on one project.
    peak, peak_day = 0, None
    by_project = defaultdict(list)
    for s, iv in timed:
        for x0, x1 in iv:
            by_project[s.project or "?"] += [(x0, 1), (x1, -1)]
    for events in by_project.values():
        n = 0
        for ts, step in sorted(events, key=lambda e: (e[0], e[1])):
            n += step
            if n > peak:
                peak, peak_day = n, fmt_day(ts)
    foreign = [(s, f) for s in sessions for f in s.foreign]
    pairs.sort(key=lambda p: -p["minutes"])
    return {
        "pairs": len(pairs), "sessions": len(involved), "days": len(days),
        "peak": peak, "peak_day": peak_day, "by_project": per_project.most_common(5), "examples": pairs[:examples],
        "foreign": {
            "mentions": len(foreign), "sessions": len({s.id for s, _ in foreign}),
            "in_overlapping": len({s.id for s, _ in foreign if s.id in involved}),
            "examples": [{"date": fmt_day(ts or s.start), "session": s.id[:8], "project": s.project, "who": who, "text": text}
                         for s, (ts, who, text) in sorted(foreign, key=lambda x: x[1][0] or NOW, reverse=True)[:examples]],
        },
    }


def unscoped_report(unscoped, limit=8, chars=120):
    """Sessions set aside by --project because their store does not say which folder they belong to."""
    out = []
    for s in sorted(unscoped, key=lambda x: x.start or NOW, reverse=True)[:limit]:
        out.append({"session": s.id[:8], "source": s.source, "project": s.project, "date": fmt_day(s.start),
                    "prompts": len(s.prompts()), "first": [excerpt(t.text, chars) for t in s.prompts()[:3]]})
    return {"count": len(unscoped), "sessions": out}


def missing_project_dirs(sessions, limit=3):
    """Working directories recorded in the sessions that do not exist here (history copied from another
    machine, moved repo): the repo's existing instructions, prompts and skills cannot be looked up there."""
    missing = Counter()
    for s in sessions:
        for c in (s.cwds | {s.cwd}):
            if c and not os.path.isdir(c):
                missing[c] += 1
    return [c for c, _n in missing.most_common(limit)], len(missing)


CONFIRM_YES_RE = re.compile(r"^(oui|ouais|yes|yep|ok|okay|go|vas[- ]?y|fais[- ]?le|fais|continue|d'accord|parfait|top|"
                            r"valide|commit|commite|push|pousse|merge|fusionne)\b")
ASK_PATTERNS = [
    ("git", re.compile(r"\b(commit|commite|push|pousse|merge|fusionn|tag)")),
    ("action", re.compile(r"\b(veux[- ]tu|tu veux|souhaites[- ]tu|voulez[- ]vous|je (le |la |les |l')?(fais|lance|continue|"
                          r"attaque|corrige|applique|cree|ajoute|supprime|mets|passe)|on (y )?va|j'y vais|should i|"
                          r"do you want|shall i|want me to|would you like)")),
]


def assistant_questions(sessions, examples=3):
    """Replies that end by asking permission ("je commite ?"). Each one answered by a bare "oui" is a turn
    the user could have saved with a standing rule."""
    stats = {}
    for s in sessions:
        for i, t in enumerate(s.turns[:-1]):
            reply = (t.reply or "").strip().rstrip("*_` \n")
            if not reply.endswith("?"):
                continue
            last = re.split(r"(?<=[.!?\n])\s+", reply)[-1]
            n = normalize(last)
            cat = next((c for c, rx in ASK_PATTERNS if rx.search(n)), None)
            if not cat:
                continue
            e = stats.setdefault(cat, {"questions": 0, "answered_yes": 0, "sessions": set(), "examples": []})
            e["questions"] += 1
            e["sessions"].add(s.id)
            nxt = s.turns[i + 1]
            if nxt.kind == "prompt" and len(nxt.text) <= 80 and CONFIRM_YES_RE.match(normalize(nxt.text)):
                e["answered_yes"] += 1
                if len(e["examples"]) < examples:
                    e["examples"].append({"question": excerpt(last, 110), "answer": excerpt(nxt.text, 60),
                                          "session": s.id[:8], "date": fmt_day(nxt.ts or s.start)})
    for e in stats.values():
        e["sessions"] = len(e["sessions"])
    return stats


def declined_offers(sessions, examples=4):
    """"non" answering a proposal of the assistant: not a correction, but a sign that it offered work
    (often code) the user did not ask for."""
    total, sess, ex = 0, set(), []
    for s in sessions:
        for i, t in enumerate(s.turns):
            if "REFUS_PROPOSITION" not in t.flags:
                continue
            total += 1
            sess.add(s.id)
            if len(ex) < examples:
                reply = (s.turns[i - 1].reply or "").strip().rstrip("*_` \n")
                ex.append({"date": fmt_day(t.ts or s.start), "session": s.id[:8], "project": s.project,
                           "question": excerpt(re.split(r"(?<=[.!?\n])\s+", reply)[-1], 110),
                           "answer": excerpt(t.text, 90)})
    return {"count": total, "sessions": len(sess), "examples": ex}


def tool_usage(sessions, limit=10):
    calls, errors, sess = Counter(), Counter(), defaultdict(set)
    for s in sessions:
        for t in s.turns:
            for name, n in t.tools.items():
                calls[tool_family(name)] += n
                sess[tool_family(name)].add(s.id)
            for name, n in t.error_tools.items():
                errors[tool_family(name)] += n
    return [{"tool": f, "calls": c, "sessions": len(sess[f]), "errors": errors[f]} for f, c in calls.most_common(limit)]


def tool_inputs(sessions, limit=6, chars=110):
    """Code-like tool inputs (MCP scripts, queries…): rewritten every time, or repeated verbatim."""
    per_tool = defaultdict(lambda: {"calls": 0, "distinct": set(), "sessions": set()})
    same = defaultdict(lambda: {"calls": 0, "sessions": set(), "example": ""})
    for s in sessions:
        for t in s.turns:
            for name, fp, text in t.inputs:
                if "\n" in text.strip() or len(text) >= 120:  # code or query, not a click or a URL
                    p = per_tool[name]
                    p["calls"] += 1
                    p["distinct"].add(fp)
                    p["sessions"].add(s.id)
                e = same[(name, fp)]
                e["calls"] += 1
                e["sessions"].add(s.id)
                e["example"] = e["example"] or text
    rewritten = [{"tool": k, "calls": v["calls"], "distinct": len(v["distinct"]), "sessions": len(v["sessions"])}
                 for k, v in per_tool.items() if v["calls"] >= 20 and len(v["sessions"]) >= 2]
    rewritten.sort(key=lambda r: -r["calls"])
    repeated = [{"tool": k[0], "calls": v["calls"], "sessions": len(v["sessions"]), "example": excerpt(v["example"], chars)}
                for k, v in same.items() if v["calls"] >= 3 and len(v["sessions"]) >= 2]
    repeated.sort(key=lambda r: (-r["sessions"], -r["calls"]))
    return {"rewritten": rewritten[:limit], "repeated": repeated[:limit]}


def project_memories(sessions, limit=15):
    """Claude Code auto-memory notes of the analysed projects. A long note that keeps growing is often a
    procedure that belongs in a skill."""
    out = []
    for d in sorted({os.path.dirname(s.path) for s in sessions if s.source == "claude"}):
        for p in glob.glob(os.path.join(d, "memory", "*.md")):
            if os.path.basename(p).upper() == "MEMORY.MD":
                continue
            _name, desc = read_frontmatter(p)
            if not desc:
                try:
                    with open(p, encoding="utf-8", errors="replace") as f:
                        desc = next((ln.strip("# \n") for ln in f if ln.strip() and not ln.startswith("---")), "")
                except OSError:
                    desc = ""
            out.append({"project": os.path.basename(d), "file": os.path.basename(p),
                        "bytes": os.path.getsize(p), "summary": excerpt(desc, 90)})
    out.sort(key=lambda m: -m["bytes"])
    return out[:limit]


def grep_report(sessions, pattern, in_replies=False, limit=8, chars=150):
    """Check a hypothesis ("do I often ask for X?") with numbers instead of reading sessions."""
    try:
        rx = re.compile(pattern, re.I)
    except re.error as e:
        return "Motif invalide : %s" % e
    hits = []
    for s in sessions:
        for i, t in enumerate(s.turns):
            text = t.reply if in_replies else t.text
            if not text:
                continue
            m = rx.search(text) or rx.search(normalize(text))
            if m:
                hits.append((s, i, t, text, m))
    where = "réponses de l'assistant" if in_replies else "demandes de l'utilisateur"
    if not hits:
        return "Motif « %s » : aucune occurrence dans les %s (%d sessions lues)." % (pattern, where, len(sessions))
    sess = {s.id for s, *_ in hits}
    days = {fmt_day(t.ts or s.start) for s, _i, t, *_ in hits}
    stamps = sorted(t.ts or s.start for s, _i, t, *_ in hits if (t.ts or s.start))
    L = ["# Motif « %s » dans les %s" % (pattern, where),
         "- %d occurrences · %d sessions sur %d · %d jours distincts%s"
         % (len(hits), len(sess), len(sessions), len(days),
            " · du %s au %s" % (fmt_day(stamps[0]), fmt_day(stamps[-1])) if stamps else ""),
         "- par projet : " + ", ".join("%s (%d)" % kv for kv in Counter(s.project or "?" for s, *_ in hits).most_common(8)),
         "- par source : " + ", ".join("%s (%d)" % kv for kv in Counter(s.source for s, *_ in hits).most_common()),
         "", "Exemples (les plus récents, une session chacun). Lis-les avant de citer le chiffre : un seul exemple hors "
             "sujet veut dire que le motif est trop large, resserre-le et relance.", ""]
    shown = set()
    for s, i, t, text, m in sorted(hits, key=lambda h: h[2].ts or h[0].start or NOW, reverse=True):
        if s.id in shown:
            continue
        shown.add(s.id)
        raw = rx.search(text)  # m may come from the accent-free text, whose offsets differ
        start = max(0, raw.start() - chars // 3) if raw else 0
        L.append("- [%s · %s · session %s · tour %d] « %s%s »"
                 % (fmt_day(t.ts or s.start), s.project, s.id[:8], i + 1, "…" if start else "",
                    excerpt(text[start:], chars)))
        if len(shown) >= limit:
            break
    return "\n".join(L)


def build_digest(sessions, sources, args):
    idf, df, n_prompts = compute_idf(sessions)
    correction_labels = Counter()
    for s in sessions:
        correction_labels.update(session_metrics(s, idf))
    clusters = cluster_prompts(sessions, idf, df, n_prompts, args.similarity, args.min_sessions,
                               args.max_clusters, args.excerpt_chars)
    rounds = sorted(s.metrics["rounds"] for s in sessions) or [0]
    starts = [s.start for s in sessions if s.start]
    ranked = sorted(sessions, key=lambda s: (-s.metrics["friction"], -s.metrics["rounds"]))
    commands, skills_used = Counter(), Counter()
    for s in sessions:
        commands.update(s.commands)
        skills_used.update(s.skills)
    project_dirs = project_roots(sessions)
    missing, n_missing = missing_project_dirs(sessions)
    if missing:
        WARNINGS.append("%d dossier(s) de projet enregistré(s) dans les sessions n'existent pas sur cette machine (ex. %s) : "
                        "l'existant de ces dépôts (instructions, prompts, skills) n'a été cherché que dans le profil et "
                        "dans --cwd (%s). Si ce n'est pas le dossier du dépôt, relancez avec `--cwd <dossier du dépôt>` ; "
                        "à défaut, ne concluez pas que rien n'existe." % (n_missing, ", ".join(missing), args.cwd))
    return {
        "generated_at": NOW.isoformat(timespec="seconds"),
        "sources": sorted(sources),
        "filters": {"days": args.days, "project": args.project},
        # --cwd only says where to look for existing skills / instructions: it is not the scope of the audit.
        "existing_lookup": {"cwd": args.cwd, "cwd_default": getattr(args, "cwd_default", False),
                            "cwd_project": project_name(args.cwd)},
        "overview": {
            "sessions": len(sessions),
            "prompts": sum(s.metrics["prompts"] for s in sessions),
            "first_day": fmt_day(min(starts)) if starts else "?",
            "last_day": fmt_day(max(starts)) if starts else "?",
            "median_rounds": rounds[len(rounds) // 2],
            "sessions_8plus_rounds": sum(1 for r in rounds if r >= 8),
            "sessions_with_corrections": sum(1 for s in sessions if s.metrics["corrections"]),
            "by_source": dict(Counter(s.source for s in sessions)),
            "by_client": dict(Counter(s.client for s in sessions)),
            "nitrite_sessions": sum(1 for s in sessions if s.client == "jetbrains-nitrite"),
            "by_project": dict(Counter(s.project or "?" for s in sessions).most_common(12)),
            "projects": len({s.project or "?" for s in sessions}),
        },
        "high_friction_sessions": [
            dict(id=s.id, short_id=s.id[:8], source=s.source, project=s.project, date=fmt_day(s.start),
                 title=excerpt(s.title or "", 80), first_prompt=excerpt(s.prompts()[0].text if s.prompts() else "",
                                                                        args.excerpt_chars),
                 **s.metrics)
            for s in ranked[: args.top_sessions] if s.metrics["friction"] > 0
        ],
        "friction_episodes": friction_episodes(sessions, args.episodes),
        "guardrails": guardrail_report(sessions),
        "defects": defect_report(sessions),
        "assistant_questions": assistant_questions(sessions),
        "declined_offers": declined_offers(sessions),
        "tool_errors": tool_error_report(sessions),
        "tool_error_causes": tool_error_causes(sessions),
        "overlaps": overlap_report(sessions),
        # With --project they are set aside; without it they are counted but their requests are easy to miss.
        "unscoped": unscoped_report(getattr(args, "unscoped", []) if args.project
                                    else [s for s in sessions if unknown_project(s)]),
        "unscoped_filtered": bool(args.project),
        "warnings": list(dict.fromkeys(WARNINGS)),
        "missing_project_dirs": n_missing,
        "recurring_requests": clusters,
        "recurring_shell_commands": recurring_commands(sessions, args.excerpt_chars),
        "tool_usage": tool_usage(sessions),
        "tool_inputs": tool_inputs(sessions),
        "memories": project_memories(sessions),
        "correction_phrasings": correction_labels.most_common(10),
        "slash_commands_used": commands.most_common(15),
        "skills_used": skills_used.most_common(15),
        "existing_skills": existing_skills(sources, args.claude_home, args.copilot_home, args.cwd, project_dirs),
        "existing_commands": existing_commands(sources, args.claude_home, args.cwd, project_dirs),
        "instruction_files": instruction_files(sources, args.claude_home, args.cwd, project_dirs),
    }


def project_roots(sessions, limit=40):
    """Working directories of the analysed sessions, plus their parents up to the git root,
    so project-level skills / commands / instruction files are found even from a sub-folder."""
    roots = []
    seen = Counter()
    for s in sessions:
        seen.update(c for c in (s.cwds | {s.cwd}) if c)
    for cwd, _n in seen.most_common():
        d = os.path.normpath(cwd)
        for _ in range(4):
            if not os.path.isdir(d):
                break
            if d not in roots:
                roots.append(d)
            if os.path.exists(os.path.join(d, ".git")) and os.path.isdir(os.path.join(d, ".claude")) \
                    or os.path.dirname(d) == d:
                break
            d = os.path.dirname(d)
    return roots[:limit]


def md_table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c).replace("|", "\\|") for c in r) + " |")
    return "\n".join(out)


def render_markdown(d):
    o = d["overview"]
    L = []
    L.append("# Digest d'historique — %s" % ", ".join(d["sources"]))
    L.append("")
    L.append("Généré le %s · période : %s → %s · filtres : %s jours, projet=%s · données lues localement, rien n'a été envoyé."
             % (d["generated_at"][:16], o["first_day"], o["last_day"], d["filters"]["days"] or "tous",
                d["filters"]["project"] or "tous"))
    L.append("")
    L.append("## Vue d'ensemble")
    L.append("- Sessions : **%d** (%s)" % (o["sessions"], ", ".join("%s=%d" % kv for kv in o["by_source"].items())))
    L.append("- Requêtes utilisateur (prompts) : **%d**" % o["prompts"])
    L.append("- Tours par session (médiane) : **%d** · sessions ≥ 8 tours : **%d** · sessions avec au moins une correction : **%d**"
             % (o["median_rounds"], o["sessions_8plus_rounds"], o["sessions_with_corrections"]))
    others = o.get("projects", len(o["by_project"])) - len(o["by_project"])
    rest = sum(o["by_project"].values())
    L.append("- Projets (**%d**) : " % o.get("projects", len(o["by_project"]))
             + ", ".join("%s (%d)" % kv for kv in o["by_project"].items())
             + (" + %d autres (%d sessions)" % (others, o["sessions"] - rest) if others > 0 else ""))
    scope = ("projet=%s" % d["filters"]["project"]) if d["filters"]["project"] else "tous les projets ci-dessus"
    L.append("- Périmètre de l'audit : **%s**. Le dossier `--cwd` ne sert qu'à chercher l'existant (section « Déjà en place ») "
             "et ne restreint pas ce périmètre." % scope)
    L.append("")

    L.append("## Sessions à forte friction (là où ça a tourné en rond)")
    L.append("Score = 2×corrections + 2×reformulations + 2×interruptions + 1,5×défauts signalés + 1,5×votes négatifs + garde-fous + refus d'outil + échecs + 0,5×erreurs d'outil (plafonnées à 10). C'est un classement, pas un verdict : les épisodes ci-dessous donnent le contexte.")
    L.append("")
    if d["high_friction_sessions"]:
        L.append(md_table(
            ["id", "source", "projet", "date", "tours", "corr.", "défauts", "garde-fous", "reform.", "interr.", "refus",
             "err. outils", "score", "titre / 1re demande"],
            [[s["short_id"], s["source"], s["project"] or "?", s["date"], s["rounds"], s["corrections"], s["defects"],
              s["guardrails"], s["rephrasings"], s["interruptions"], s["tool_rejections"], s["tool_errors"], s["friction"],
              s["title"] or s["first_prompt"]]
             for s in d["high_friction_sessions"]]))
    else:
        L.append("_Aucune session avec des signaux de friction._")
    L.append("")

    L.append("## Épisodes de friction, avec leur contexte")
    L.append("Pour chacun : la demande, la fin de la réponse et les outils utilisés, puis la réaction de l'utilisateur. "
             "C'est en général assez pour nommer la cause sans ouvrir la session.")
    L.append("")
    if not d["friction_episodes"]:
        L.append("_Aucun._")
    for e in d["friction_episodes"]:
        L.append("- **%s** · %s · %s · session %s · tour %d" % (", ".join(e["kinds"]), e["date"], e["project"], e["session"], e["turn"]))
        L.append("  - demandé : « %s »" % e["asked"])
        if e["reply"]:
            L.append("  - fin de réponse : « %s » (%s)" % (e["reply"], e["tools"]))
        else:
            L.append("  - outils : %s" % e["tools"])
        if e["reaction"]:
            L.append("  - réaction : « %s »" % e["reaction"])
    L.append("")

    L.append("## Garde-fous tapés par l'utilisateur")
    g = d["guardrails"]
    if not g:
        L.append("_Aucun._")
    else:
        L.append("Consignes préventives (« sans coder », « ne fais pas le build », « attends mon go »…), fautes de frappe comprises. "
                 "Une consigne qui revient est une règle que l'assistant devrait déjà connaître.")
        for cat, e in g.items():
            L.append("- %s : %d fois dans %d sessions" % (cat, e["count"], e["sessions"]))
            for x in e["examples"]:
                L.append("  - [%s · %s · session %s · tour %d] « %s »" % (x["date"], x["project"], x["session"], x["turn"], x["text"]))
    L.append("")

    L.append("## Défauts signalés par l'utilisateur après une livraison")
    df_ = d["defects"]
    if not df_["count"]:
        L.append("_Aucun._")
    else:
        L.append("%d signalements dans %d sessions (« ça dépasse », « ne s'affiche pas », « il y a un bug »…). "
                 "Si l'assistant avait vérifié son travail, ces tours n'auraient pas eu lieu." % (df_["count"], df_["sessions"]))
        for x in df_["examples"]:
            L.append("- [%s · %s · session %s · tour %d] « %s »%s"
                     % (x["date"], x["project"], x["session"], x["turn"], x["text"],
                        " ← après « %s »" % x["after"] if x["after"] else ""))
    L.append("")

    L.append("## Questions de confirmation posées par l'assistant")
    q = d["assistant_questions"]
    if not q:
        L.append("_Aucune (ou réponses de l'assistant non disponibles pour cette source)._")
    else:
        L.append("Chaque question suivie d'un simple « oui », « ok », « push »… est un tour qu'une règle d'autonomie aurait évité.")
        for cat, label in (("git", "commit / push / merge"), ("action", "« je le fais ? », « tu veux que… ? »")):
            if cat in q:
                e = q[cat]
                L.append("- %s : %d questions dans %d sessions, dont **%d suivies d'un simple accord**"
                         % (label, e["questions"], e["sessions"], e["answered_yes"]))
                for x in e["examples"]:
                    L.append("  - [%s · session %s] « %s » → « %s »" % (x["date"], x["session"], x["question"], x["answer"]))
    dec = d.get("declined_offers") or {}
    if dec.get("count"):
        L.append("- « non » en réponse à une proposition de l'assistant : %d fois dans %d sessions. **Pas compté comme "
                 "correction** (l'assistant n'avait encore rien fait de travers), mais souvent le signe qu'il propose "
                 "du travail non demandé :" % (dec["count"], dec["sessions"]))
        for x in dec["examples"]:
            L.append("  - [%s · %s · session %s] « %s » → « %s »" % (x["date"], x["project"], x["session"], x["question"], x["answer"]))
    L.append("")

    L.append("## Demandes récurrentes (groupes de prompts similaires)")
    L.append("Groupes construits par similarité lexicale : à valider en lisant les exemples (faux positifs possibles, groupes à fusionner ou scinder).")
    L.append("")
    if not d["recurring_requests"]:
        L.append("_Aucun groupe de demandes similaires détecté sur cette période._")
    for i, c in enumerate(d["recurring_requests"], 1):
        L.append("### G%d — %s" % (i, ", ".join(c["terms"])))
        L.append("- %d prompts · %d sessions · %d jours distincts · %d en ouverture de session · du %s au %s"
                 % (c["prompts"], c["sessions"], c["days"], c["openers"], c["first_seen"], c["last_seen"]))
        L.append("- Projets : %s · sources : %s" % (", ".join(c["projects"]), ", ".join(c["sources"])))
        L.append("- Sessions concernées : %s tours en moyenne, friction moyenne %s"
                 % (c["avg_session_rounds"], c["avg_session_friction"]))
        for e in c["examples"]:
            L.append("  - [%s · %s · session %s · tour %d] « %s »" % (e["date"], e["project"], e["session"], e["turn"], e["text"]))
        L.append("")

    L.append("## Commandes shell récurrentes (≥ 3 sessions)")
    if d["recurring_shell_commands"]:
        L.append(md_table(["commande", "sessions", "exécutions", "exemple"],
                          [[r["command"], r["sessions"], r["count"], "`%s`" % r["example"]] for r in d["recurring_shell_commands"]]))
    else:
        L.append("_Rien de notable (les commandes d'exploration comme ls, cat, grep, git status sont ignorées)._")
    L.append("")

    L.append("## Outils les plus utilisés")
    if d["tool_usage"]:
        L.append(md_table(["outil", "appels", "sessions", "erreurs"],
                          [[r["tool"], r["calls"], r["sessions"], r["errors"]] for r in d["tool_usage"]]))
    ti = d["tool_inputs"]
    if ti["rewritten"]:
        L.append("")
        L.append("Code ou requêtes écrits à la main dans un outil (beaucoup de contenus différents = une aide réutilisable manque peut-être) :")
        for r in ti["rewritten"]:
            L.append("- %s : %d appels, %d contenus différents, %d sessions" % (r["tool"], r["calls"], r["distinct"], r["sessions"]))
    if ti["repeated"]:
        L.append("")
        L.append("Contenus identiques renvoyés plusieurs fois :")
        for r in ti["repeated"]:
            L.append("- %s : %d fois dans %d sessions · `%s`" % (r["tool"], r["calls"], r["sessions"], r["example"]))
    if d["tool_errors"]:
        L.append("")
        L.append("Erreurs d'outils les plus fréquentes (première ligne du message) :")
        for r in d["tool_errors"]:
            L.append("- %s : %d fois dans %d sessions · « %s »" % (r["tool"], r["count"], r["sessions"], r["example"]))
    L.append("")

    L.append("## Coût côté assistant : erreurs d'outils par cause")
    causes = d.get("tool_error_causes") or []
    if not causes:
        L.append("_Aucune erreur d'outil._")
    else:
        L.append("L'utilisateur ne voit pas ces échecs et ne s'en plaint pas : ils coûtent du temps et des tokens à chaque session. "
                 "Une cause qui revient (environnement, dossier, syntaxe du shell, script qui échoue sans message) "
                 "se corrige souvent une fois pour toutes, par une règle ou en réparant le script du projet.")
        for r in causes:
            L.append("- **%s** : %d erreurs dans %d sessions · outils : %s%s"
                     % (r["label"], r["count"], r["sessions"], ", ".join("%s (%d)" % kv for kv in r["tools"]),
                        " · commandes : " + ", ".join("`%s` (%d)" % kv for kv in r["commands"]) if r["commands"] else ""))
            for x in r["examples"][:2]:
                L.append("  - [%s · session %s · %s] %s« %s »" % (x["date"], x["session"], x["tool"],
                                                                 "`%s` → " % x["command"] if x["command"] else "", x["error"]))
    L.append("")

    L.append("## Sessions en parallèle sur le même projet")
    ov = d.get("overlaps") or {}
    fo = ov.get("foreign") or {}
    if not ov.get("pairs") and not fo.get("mentions"):
        L.append("_Aucun chevauchement détecté._")
    else:
        L.append("Deux sessions actives en même temps dans les mêmes dossiers voient chacune les modifications non commitées "
                 "de l'autre, sans savoir à qui elles sont : commits qui embarquent le travail d'une autre session, "
                 "« c'est quoi ce fichier modifié ? », serveurs arrêtés par une autre session.")
        L.append("- %d paires de sessions qui se chevauchent (≥ 2 min), %d sessions concernées, sur %d jours ; "
                 "jusqu'à %d sessions actives en même temps%s"
                 % (ov["pairs"], ov["sessions"], ov["days"], ov["peak"], " (le %s)" % ov["peak_day"] if ov["peak_day"] else ""))
        if ov.get("by_project"):
            L.append("- Par projet : " + ", ".join("%s (%d paires)" % kv for kv in ov["by_project"]))
        for p in ov.get("examples", [])[:3]:
            L.append("  - [%s · %s] sessions %s : %d min en commun" % (p["date"], p["project"], " et ".join(p["sessions"]), p["minutes"]))
        if fo.get("mentions"):
            L.append("- Modifications attribuées à une autre session ou à quelqu'un d'autre : %d mentions dans %d sessions "
                     "(dont %d qui en chevauchent une autre)" % (fo["mentions"], fo["sessions"], fo["in_overlapping"]))
            for x in fo["examples"]:
                L.append("  - [%s · %s · session %s · %s] « %s »" % (x["date"], x["project"], x["session"], x["who"], x["text"]))
    L.append("")

    un = d.get("unscoped") or {}
    if un.get("count"):
        if d.get("unscoped_filtered"):
            L.append("## Sessions sans projet identifié (écartées par --project)")
            L.append("Le stockage ne dit pas à quel dossier elles appartiennent (fenêtre VS Code vide, ancienne base JetBrains). "
                     "Vérifiez si elles concernent le projet analysé avant de les ignorer.")
        else:
            L.append("## Sessions sans projet identifié (comptées dans les chiffres)")
            L.append("Le stockage ne dit pas à quel dossier elles appartiennent (fenêtre VS Code vide, ancienne base JetBrains). "
                     "Leurs demandes sont souvent courtes et isolées : classez-les quand même (thème récurrent, ou "
                     "question ponctuelle à mettre dans « ce qui ne mérite pas d'automatisation »).")
        for x in un["sessions"]:
            L.append("- [%s · %s · session %s · %d demandes] %s" % (x["date"], x["source"], x["session"], x["prompts"],
                                                                     " / ".join("« %s »" % f for f in x["first"])))
        L.append("")

    if d["memories"]:
        L.append("## Notes de mémoire des projets (les plus volumineuses d'abord)")
        L.append("Une note longue et qui grossit décrit souvent une procédure : sa place est peut-être dans un skill.")
        for m in d["memories"]:
            L.append("- %s/%s (%.1f Ko) : %s" % (m["project"], m["file"], m["bytes"] / 1024, m["summary"]))
        L.append("")

    L.append("## Formulations de correction les plus fréquentes")
    if d["correction_phrasings"]:
        for label, n in d["correction_phrasings"]:
            L.append("- %s : %d" % (label, n))
    else:
        L.append("_Aucune._")
    L.append("")

    L.append("## Déjà en place")
    none = "aucun trouvé (non vérifié dans les dépôts introuvables, voir les limites)" if d.get("missing_project_dirs") else "aucun"
    L.append("- Commandes slash utilisées : " + (", ".join("/%s (%d)" % kv for kv in d["slash_commands_used"]) or "aucune"))
    L.append("- Skills invoqués : " + (", ".join("%s (%d)" % kv for kv in d["skills_used"]) or "aucun détecté"))
    L.append("- Skills disponibles (profil, plugins, projets analysés) : "
             + (", ".join(s["name"] for s in d["existing_skills"]) or none))
    L.append("- Commandes / prompt files définis : "
             + (", ".join("%s (%s)" % (c["name"], c["location"]) for c in d["existing_commands"]) or none))
    L.append("- Fichiers d'instructions (profil + projets analysés) : " + (", ".join(d["instruction_files"]) or none))
    lk = d.get("existing_lookup")
    if lk:
        L.append("- Dossier `--cwd` fouillé en plus du profil et des projets analysés : %s (%s). Il sert à trouver l'existant, "
                 "pas à choisir les projets audités." % (lk["cwd"], "dossier courant, par défaut" if lk["cwd_default"]
                                                         else "passé explicitement"))
    L.append("")
    L.append("## Limites")
    L.append("- Heuristiques lexicales (FR/EN) : une correction peut être manquée ou mal détectée ; les tours ne disent pas *pourquoi* ça a tourné.")
    L.append("- Seul ce qui est stocké localement est lu (pas Copilot sur github.com, Visual Studio ou Xcode, ni les sessions supprimées).")
    if d["overview"].get("nitrite_sessions"):
        L.append("- Sessions JetBrains de la base native (%d) : lecture best-effort d'un format binaire non documenté ; "
                 "dates = date du fichier ; en mode Ask les demandes ne sont pas récupérées (seulement en mode Agent)."
                 % d["overview"]["nitrite_sessions"])
    L.append("- La session d'audit en cours peut apparaître dans les résultats : l'ignorer (ou `--exclude-session <id>`).")
    for w in d.get("warnings", []):
        L.append("- " + w)
    return "\n".join(L) + "\n"


def show_session(sessions, prefix, idf_sessions, full=False):
    """Turn-by-turn view of one session. Compact by default: only flagged turns and the turns around them."""
    matches = [s for s in sessions if s.id.lower().startswith(prefix.lower())]
    if not matches:
        return "Aucune session ne commence par « %s »." % prefix
    if len(matches) > 1:
        return "Plusieurs sessions correspondent : " + ", ".join("%s (%s, %s)" % (s.id, s.source, s.project) for s in matches[:10])
    s = matches[0]
    idf, _df, _n = compute_idf(idf_sessions)
    session_metrics(s, idf)
    keep = set(range(len(s.turns)))
    if not full:
        keep = {0}
        for i, t in enumerate(s.turns):
            if t.flags:
                keep |= {i, i - 1} if set(t.flags) & REACTION_FLAGS else {i, i + 1}
    L = ["# Session %s" % s.id,
         "- source : %s · projet : %s · branche : %s · %s → %s" % (s.source, s.project, s.branch or "?", fmt_dt(s.start), fmt_dt(s.end)),
         "- titre : %s" % (s.title or "?"),
         "- " + ", ".join("%s=%s" % kv for kv in s.metrics.items()),
         "- fichier : %s" % s.path]
    if not full:
        L.append("- vue compacte : %d tours sur %d (tours signalés et leur contexte) ; `--full` pour tout afficher"
                 % (len([i for i in keep if 0 <= i < len(s.turns)]), len(s.turns)))
    L.append("")
    skipped = []
    for i, t in enumerate(s.turns):
        if i not in keep:
            skipped.append(i + 1)
            continue
        if skipped:
            L.append("_… tours %d à %d sans signal (%d) …_" % (skipped[0], skipped[-1], len(skipped)))
            L.append("")
            skipped = []
        flags = (" **[" + ", ".join(t.flags) + "]**") if t.flags else ""
        L.append("## Tour %d · %s%s%s" % (i + 1, fmt_dt(t.ts), " · commande" if t.kind == "command" else "", flags))
        L.append("> " + excerpt(t.text, 500 if full else 300))
        if t.reply:
            L.append("- fin de réponse : « %s »" % tail_excerpt(t.reply, 300 if full else 160))
        L.append("- outils : %s" % tools_summary(t, 6 if full else 4))
        for c in t.shell[: 5 if full else 2]:
            L.append("  - `%s`" % excerpt(c, 160))
        L.append("")
    if skipped:
        L.append("_… tours %d à %d sans signal (%d) …_" % (skipped[0], skipped[-1], len(skipped)))
    return "\n".join(L)


def list_sources(args):
    L = ["Sources d'historique détectées :"]
    claude_files = glob.glob(os.path.join(args.claude_home, "projects", "*", "*.jsonl"))
    L.append("- claude      : %d transcripts dans %s" % (len(claude_files), os.path.join(args.claude_home, "projects")))
    cli = glob.glob(os.path.join(args.copilot_home, "session-state", "*", "events.jsonl"))
    db = os.path.join(args.copilot_home, "session-store.db")
    L.append("- copilot-cli : %d sessions dans %s%s" % (len(cli), os.path.join(args.copilot_home, "session-state"),
                                                       " (+ session-store.db)" if os.path.isfile(db) else ""))
    dirs = vscode_user_dirs(args.vscode_user_dir)
    if not dirs:
        L.append("- vscode      : aucun dossier utilisateur VS Code trouvé")
    for d in dirs:
        n = len(glob.glob(os.path.join(d, "workspaceStorage", "*", "chatSessions", "*.json*")))
        n += len(glob.glob(os.path.join(d, "globalStorage", "emptyWindowChatSessions", "*.json*")))
        L.append("- vscode      : %d fichiers de session dans %s" % (n, d))
    root = jetbrains_root(args.jetbrains_dir)
    L.append("- jetbrains   : %d bases de chat dans %s (lecture best-effort ; détail : --jetbrains-diagnose)"
             % (len(jetbrains_dbs(root)), root))
    return "\n".join(L)


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Audit local Claude Code / GitHub Copilot chat history.")
    ap.add_argument("--source", default="all", choices=sorted(SOURCE_ALIASES),
                    help="claude, copilot (= copilot-cli + vscode), copilot-cli, vscode, all")
    ap.add_argument("--days", type=int, default=30, help="only sessions active in the last N days (0 = all)")
    ap.add_argument("--project", help="keep sessions whose project name or cwd contains this text")
    ap.add_argument("--exclude-session", action="append", help="session id prefix to ignore (repeatable)")
    ap.add_argument("--exclude-project", action="append",
                    help="ignore sessions whose project name or cwd contains this text, e.g. the audit's own folder (repeatable)")
    ap.add_argument("--format", default="md", choices=["md", "json"])
    ap.add_argument("--out", help="write the digest to this file instead of stdout")
    ap.add_argument("--show-session", metavar="ID_PREFIX",
                    help="turn-by-turn view of one session (compact: flagged turns and their context)")
    ap.add_argument("--full", action="store_true", help="with --show-session: every turn, longer excerpts")
    ap.add_argument("--grep", metavar="REGEX", help="count and sample the requests matching a pattern (case-insensitive)")
    ap.add_argument("--grep-replies", action="store_true", help="with --grep: search the assistant's replies instead")
    ap.add_argument("--grep-log", metavar="FILE",
                    help="with --grep: also append the result to FILE, so every search behind a figure is kept")
    ap.add_argument("--episodes", type=int, default=12, help="friction episodes shown in the digest")
    ap.add_argument("--list-sources", action="store_true", help="show which history locations exist and exit")
    ap.add_argument("--top-sessions", type=int, default=10)
    ap.add_argument("--max-clusters", type=int, default=15)
    ap.add_argument("--min-sessions", type=int, default=2, help="minimum distinct sessions for a recurring request")
    ap.add_argument("--similarity", type=float, default=0.4, help="prompt similarity threshold (0-1)")
    ap.add_argument("--excerpt-chars", type=int, default=160)
    ap.add_argument("--claude-home", default=os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude"))
    ap.add_argument("--copilot-home", default=os.environ.get("COPILOT_HOME") or os.path.join(HOME, ".copilot"))
    ap.add_argument("--vscode-user-dir", help="VS Code 'User' directory (auto-detected by default)")
    ap.add_argument("--jetbrains-dir", help="github-copilot folder of the JetBrains plugin (auto-detected by default)")
    ap.add_argument("--jetbrains-diagnose", action="store_true",
                    help="describe the JetBrains Copilot stores found (structure, plus 3 short excerpts per base)")
    ap.add_argument("--no-excerpts", action="store_true", help="with --jetbrains-diagnose: print no prompt text at all")
    ap.add_argument("--cwd", help="project directory used to look for skills / instruction files (default: current "
                                   "directory). It never narrows the sessions analysed: use --project for that")
    args = ap.parse_args(argv)
    args.cwd_default = not args.cwd
    args.cwd = args.cwd or os.getcwd()

    if args.list_sources:
        print(list_sources(args))
        return 0
    if args.jetbrains_diagnose:
        print(jetbrains_diagnose(args.jetbrains_dir, args.copilot_home, excerpts=not args.no_excerpts))
        return 0

    sources = SOURCE_ALIASES[args.source]
    sessions = load_sessions(sources, args)

    if args.show_session:
        text = show_session([s for s in sessions if s.turns], args.show_session, select_sessions(sessions, args),
                            full=args.full)
    elif args.grep:
        selected = select_sessions(sessions, args)
        idf, _df, _n = compute_idf(selected)
        for s in selected:
            session_metrics(s, idf)
        text = grep_report(selected, args.grep, in_replies=args.grep_replies)
        if args.grep_log:
            os.makedirs(os.path.dirname(os.path.abspath(args.grep_log)), exist_ok=True)
            with open(args.grep_log, "a", encoding="utf-8") as f:
                f.write("%s\n\n(filtres : %s jours, projet=%s)\n\n---\n\n"
                        % (text, args.days or "tous", args.project or "tous"))
    else:
        selected = select_sessions(sessions, args)
        if not selected:
            print("Aucune session trouvée pour source=%s, %s derniers jours, projet=%s.\n\n%s"
                  % (args.source, args.days or "tous les", args.project or "tous", list_sources(args)))
            return 1
        digest = build_digest(selected, sources, args)
        text = json.dumps(digest, ensure_ascii=False, indent=1) if args.format == "json" else render_markdown(digest)

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print("Digest écrit dans %s" % args.out)
    else:
        try:
            print(text)
            sys.stdout.flush()
        except (BrokenPipeError, OSError):
            pass  # output piped into `head` & co.
    return 0


if __name__ == "__main__":
    sys.exit(main())
