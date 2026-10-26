#!/usr/bin/env python3
"""Build a fake but realistic GitHub Copilot history (CLI + VS Code) to test history_audit.py.

    python tests/make_fixtures.py <output_dir>

Creates:
  <out>/copilot-home/session-state/<id>/{events.jsonl,workspace.yaml}   Copilot CLI raw sessions
  <out>/copilot-home/session-store.db                                    Copilot CLI session store (fallback)
  <out>/vscode-user/workspaceStorage/<hash>/{workspace.json,chatSessions/*}  VS Code Copilot Chat
  <out>/repo/.github/...                                                 a repo with instructions + a prompt file
  <out>/copilot-home-multi/session-state/...                             Copilot CLI history over several projects

Timestamps are relative to "now" so the default --days 30 window always matches.
Scenario: a Java team on `billing-api` keeps asking for the same JUnit tests and REST endpoints,
corrects Copilot about the same conventions (AssertJ, @DisplayName, RFC 7807), and loops on `mvn test`.
"""

import datetime as dt
import json
import os
import sqlite3
import sys

NOW = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(days_ago, minutes=0):
    return (NOW - dt.timedelta(days=days_ago) + dt.timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def ms(days_ago, minutes=0):
    return int((NOW - dt.timedelta(days=days_ago) + dt.timedelta(minutes=minutes)).timestamp() * 1000)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


# --------------------------------------------------------------------------- Copilot CLI

CLI_SESSIONS = [
    {
        "id": "1b7e44c0-0001-4000-8000-000000000001", "days": 18, "summary": "Tests InvoiceService",
        "turns": [
            ("génère les tests unitaires JUnit 5 pour InvoiceService avec Mockito, en suivant nos conventions de nommage",
             [("bash", "mvn -q test -Dtest=InvoiceServiceTest", False)]),
            ("non, utilise AssertJ et pas les assertions JUnit", [("edit", None, False)]),
            ("tu as oublié le @DisplayName en français sur chaque test", [("edit", None, False)]),
            ("lance les tests", [("bash", "mvn -q test -Dtest=InvoiceServiceTest", True),
                                 ("bash", "mvn -q test -Dtest=InvoiceServiceTest", False)]),
            ("commit avec un message conventional commits", [("bash", "git add -A && git commit -m \"test: invoice service\"", False)]),
        ],
    },
    {
        "id": "2c9f10d4-0002-4000-8000-000000000002", "days": 11, "summary": "Tests PaymentService",
        "turns": [
            ("génère les tests unitaires JUnit 5 pour PaymentService avec Mockito selon nos conventions de nommage",
             [("bash", "mvn -q test -Dtest=PaymentServiceTest", True)]),
            ("non je t'ai dit AssertJ, pas assertEquals", [("edit", None, False)]),
            ("ajoute aussi le @DisplayName en français", [("edit", None, False)]),
            ("ça ne marche toujours pas, le mock du PaymentGateway est null", [("bash", "mvn -q test -Dtest=PaymentServiceTest", True)]),
            ("il faut @ExtendWith(MockitoExtension.class) sur la classe", [("bash", "mvn -q test -Dtest=PaymentServiceTest", False)]),
            ("commit et push", [("bash", "git add -A && git commit -m \"test: payment service\" && git push", False)]),
        ],
    },
    {
        "id": "3d0a2be8-0003-4000-8000-000000000003", "days": 4, "summary": "Tests RefundService",
        "turns": [
            ("écris les tests unitaires JUnit 5 de RefundService avec Mockito et nos conventions de nommage",
             [("bash", "mvn -q test -Dtest=RefundServiceTest", False)]),
            ("non, AssertJ comme d'habitude et un @DisplayName en français", [("edit", None, False)]),
            ("commit", [("bash", "git add -A && git commit -m \"test: refund service\"", False)]),
        ],
    },
]


def cli_events(sess, project="billing-api"):
    ev = [{"type": "session.start", "timestamp": iso(sess["days"]),
           "data": {"context": {"cwd": "/home/dev/" + project, "branch": "main", "repository": "acme/" + project}}}]
    minute = 1
    for prompt, tools in sess["turns"]:
        ev.append({"type": "user.message", "timestamp": iso(sess["days"], minute), "data": {"content": prompt}})
        for name, cmd, failed in tools:
            minute += 1
            args = {"command": cmd} if cmd else {"path": "src/test/java/Foo.java"}
            ev.append({"type": "tool.execution_start", "timestamp": iso(sess["days"], minute),
                       "data": {"toolCallId": "t%d" % minute, "toolName": name, "arguments": args}})
            done = {"toolCallId": "t%d" % minute, "success": not failed}
            if failed:
                done["error"] = {"message": "Process exited with code 1: Tests run: 4, Failures: 2"}
            ev.append({"type": "tool.execution_complete", "timestamp": iso(sess["days"], minute), "data": done})
        ev.append({"type": "assistant.message", "timestamp": iso(sess["days"], minute + 1), "data": {"content": "Fait."}})
        minute += 3
    return "\n".join(json.dumps(e, ensure_ascii=False) for e in ev) + "\n"


def intellij_agent_session(home):
    """An IntelliJ chat run through the Copilot CLI agent, with the field names seen in a real 1.x store:
    sub-agent prompt and chatter, permission events, final answer ending with a question, typo guardrail."""
    sid = "8f0e1d2c-5555-4000-8000-00000000000d"
    d = os.path.join(home, "session-state", sid)
    write(os.path.join(d, "workspace.yaml"),
          "id: %s\ncwd: e:\\dev\\billing-api\ngit_root: E:\\dev\\billing-api\nrepository: acme/billing-api\n"
          "host_type: github\nbranch: main\nclient_name: copilot-intellij\nname: Revue de la sécurité\n"
          "user_named: false\ncreated_at: %s\n" % (sid, iso(3)))
    ev = [{"type": "session.start", "timestamp": iso(3), "id": "e0", "parentId": None,
           "data": {"producer": "copilot-agent", "context": {"cwd": "e:\\dev\\billing-api", "branch": "main"}}},
          {"type": "user.message", "timestamp": iso(3, 1), "id": "e1",
           "data": {"content": "analyse la sécurité de l'API sans cdore, dis moi juste ce que tu vois", "delivery": "idle"}},
          {"type": "tool.execution_start", "timestamp": iso(3, 2), "id": "e2", "agentId": "sub-1",
           "data": {"toolCallId": "c1", "toolName": "powershell", "arguments": {"command": "git status --short; mvn -q verify"}}},
          {"type": "permission.completed", "timestamp": iso(3, 2), "id": "e3",
           "data": {"requestId": "r1", "toolCallId": "c1", "result": {"kind": "denied-interactively-by-user"}}},
          {"type": "tool.execution_complete", "timestamp": iso(3, 2), "id": "e4",
           "data": {"toolCallId": "c1", "success": False, "result": {"content": "The user denied this tool call"}}},
          {"type": "user.message", "timestamp": iso(3, 3), "id": "e5",
           "data": {"content": "Explore the controllers and list the endpoints", "source": "agent-" + sid}},
          {"type": "assistant.message", "timestamp": iso(3, 4), "id": "e6",
           "data": {"content": "Found 12 controllers.", "parentToolCallId": "task-1"}},
          {"type": "assistant.message", "timestamp": iso(3, 5), "id": "e7",
           "data": {"content": "Deux risques moyens relevés. Je lance les tests de sécurité ?", "phase": "final_answer"}},
          {"type": "user.message", "timestamp": iso(3, 6), "id": "e8", "data": {"content": "oui"}},
          {"type": "tool.execution_start", "timestamp": iso(3, 7), "id": "e9",
           "data": {"toolCallId": "c2", "toolName": "powershell", "arguments": {"command": "mvn -q test -Dtest=SecurityTest"}}},
          {"type": "tool.execution_complete", "timestamp": iso(3, 8), "id": "e10",
           "data": {"toolCallId": "c2", "success": False, "result": {"content": "Tests run: 3, Failures: 1"}}},
          {"type": "assistant.message", "timestamp": iso(3, 9), "id": "e11",
           "data": {"content": "Un test échoue sur le filtre CORS.", "phase": "final_answer"}},
          {"type": "user.message", "timestamp": iso(3, 10), "id": "e12",
           "data": {"content": "le test d'intégration échoue encore, il y a un problème avec le CORS"}}]
    write(os.path.join(d, "events.jsonl"), "\n".join(json.dumps(e, ensure_ascii=False) for e in ev) + "\n")


def build_cli(root):
    home = os.path.join(root, "copilot-home")
    intellij_agent_session(home)
    for sess in CLI_SESSIONS:
        d = os.path.join(home, "session-state", sess["id"])
        write(os.path.join(d, "events.jsonl"), cli_events(sess))
        write(os.path.join(d, "workspace.yaml"),
              "cwd: /home/dev/billing-api\nrepository: acme/billing-api\nbranch: main\nsummary: %s\ncreated_at: %s\n"
              % (sess["summary"], iso(sess["days"])))
    # A session only known by the session store (raw events deleted).
    db = os.path.join(home, "session-store.db")
    if os.path.exists(db):
        os.remove(db)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE sessions (id TEXT, cwd TEXT, repository TEXT, branch TEXT, summary TEXT, created_at TEXT, updated_at TEXT, host_type TEXT)")
    con.execute("CREATE TABLE turns (session_id TEXT, turn_index INTEGER, user_message TEXT, assistant_response TEXT, timestamp TEXT)")
    sid = "4e1b3cf2-0004-4000-8000-000000000004"
    con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",
                (sid, "/home/dev/billing-api", "acme/billing-api", "main", "Tests CustomerService", iso(25), iso(25), "cli"))
    for i, msg in enumerate(["génère les tests unitaires JUnit 5 pour CustomerService avec Mockito selon nos conventions de nommage",
                             "non, avec AssertJ s'il te plaît"]):
        con.execute("INSERT INTO turns VALUES (?,?,?,?,?)", (sid, i, msg, "ok", iso(25, i * 5)))
    # The store also indexes sessions that still have raw events: must not be counted twice.
    con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",
                (CLI_SESSIONS[0]["id"], "/home/dev/billing-api", "acme/billing-api", "main", "dup", iso(18), iso(18), "cli"))
    con.commit()
    con.close()


# Global audit: the same user also works on two batch projects and an analysis tool. Launched from billing-api,
# a "since the beginning" audit must still see the batch theme, which spans two repositories.
MULTI_SESSIONS = [
    ("batch-exports", {
        "id": "7a000001-0101-4000-8000-000000000101", "days": 22, "summary": "Job export comptable",
        "turns": [
            ("écris le job batch d'export comptable : reader JDBC, processor, writer CSV, en chunks de 500", []),
            ("non, il faut une skip policy et la reprise sur erreur, comme pour les autres batchs", [("edit", None, False)]),
            ("et le paramètre de date d'arrêté passé en JobParameter, pas en dur", [("edit", None, False)]),
        ],
    }),
    ("batch-exports", {
        "id": "7a000002-0102-4000-8000-000000000102", "days": 9, "summary": "Job export clients",
        "turns": [
            ("écris le job batch d'export des clients : reader JDBC, writer CSV, en chunks", []),
            ("tu as encore oublié la skip policy et la reprise sur erreur", [("edit", None, False)]),
        ],
    }),
    ("batch-relances", {
        "id": "7a000003-0103-4000-8000-000000000103", "days": 15, "summary": "Job relances impayés",
        "turns": [
            ("écris le job batch de relance des impayés : reader JDBC, processor, writer vers la file d'envoi, en chunks", []),
            ("non, ajoute la skip policy et la reprise sur erreur comme d'habitude", [("edit", None, False)]),
            ("la date d'arrêté doit venir des JobParameters", [("edit", None, False)]),
        ],
    }),
    ("batch-relances", {
        "id": "7a000004-0104-4000-8000-000000000104", "days": 3, "summary": "Job purge relances",
        "turns": [
            ("écris le job batch de purge des relances anciennes, en chunks", []),
            ("il manque la skip policy et la reprise sur erreur", [("edit", None, False)]),
        ],
    }),
    ("log-analyzer", {
        "id": "7a000005-0105-4000-8000-000000000105", "days": 12, "summary": "Analyse des logs",
        "turns": [
            ("analyse ce fichier de logs et sors les 10 erreurs les plus fréquentes", []),
        ],
    }),
]


def build_multi(root):
    """A Copilot CLI home spread over several projects, for global (all projects) audits."""
    home = os.path.join(root, "copilot-home-multi")
    for project, sess in [("billing-api", x) for x in CLI_SESSIONS] + MULTI_SESSIONS:
        d = os.path.join(home, "session-state", sess["id"])
        write(os.path.join(d, "events.jsonl"), cli_events(sess, project))
        write(os.path.join(d, "workspace.yaml"),
              "cwd: /home/dev/%s\nrepository: acme/%s\nbranch: main\nsummary: %s\ncreated_at: %s\n"
              % (project, project, sess["summary"], iso(sess["days"])))


# --------------------------------------------------------------------------- VS Code

def vs_request(rid, text, days, minute, tools=(), vote=None, state=1):
    resp = [{"value": "Voici la proposition.", "supportThemeIcons": False}]
    for tool, cmd, error, denied in tools:
        part = {"kind": "toolInvocationSerialized", "toolId": tool, "toolCallId": rid + tool,
                "invocationMessage": "Running " + tool, "isComplete": True,
                "isConfirmed": {"type": 0} if denied else {"type": 4}, "source": {"type": "internal"}}
        if cmd:
            part["toolSpecificData"] = {"kind": "terminal", "commandLine": {"original": cmd}, "language": "sh"}
        if error:
            part["resultError"] = error
        resp.append(part)
    r = {"requestId": rid, "message": {"text": text, "parts": []}, "variableData": {"variables": []},
         "response": resp, "timestamp": ms(days, minute), "modelId": "copilot/auto", "modelState": {"value": state}}
    if vote is not None:
        r["vote"] = vote
    return r


def build_vscode(root):
    user = os.path.join(root, "vscode-user")
    ws = os.path.join(user, "workspaceStorage", "9f1c2b7d0e")
    write(os.path.join(ws, "workspace.json"), json.dumps({"folder": "file:///home/dev/billing-api"}))

    # Modern mutation-log session (VS Code >= 1.109).
    sid = "5f2c4d06-1111-4000-8000-00000000000a"
    initial = {"version": 3, "sessionId": sid, "creationDate": ms(9), "customTitle": "Endpoint factures",
               "requests": [], "pendingRequests": [], "hasPendingEdits": False}
    r0 = vs_request("r0", "ajoute un endpoint REST GET /invoices/{id} avec validation et erreurs au format RFC 7807", 9, 1,
                    tools=[("run_in_terminal", "mvn -q verify", "Command failed: exit code 1", False)])
    r1 = vs_request("r1", "ça ne marche pas, le ProblemDetail n'est pas renvoyé", 9, 6,
                    tools=[("run_in_terminal", "mvn -q verify", "Command failed: exit code 1", False)], vote=0)
    r2 = vs_request("r2", "toujours pas, il faut un @RestControllerAdvice global", 9, 12,
                    tools=[("run_in_terminal", "mvn -q verify", None, False)])
    lines = [
        {"kind": 0, "v": initial},
        {"kind": 2, "k": ["requests"], "v": [r0]},
        {"kind": 2, "k": ["requests"], "v": [dict(r1, response=[])]},
        {"kind": 1, "k": ["requests", 1, "response"], "v": r1["response"]},
        {"kind": 1, "k": ["requests", 1, "vote"], "v": 0},
        # A request that was edited/resent: truncate at index 2 then push the final version.
        {"kind": 2, "k": ["requests"], "v": [vs_request("r2-draft", "brouillon annulé", 9, 10)]},
        {"kind": 2, "k": ["requests"], "v": [r2], "i": 2},
        {"kind": 3, "k": ["customTitle"]},
        {"kind": 1, "k": ["customTitle"], "v": "Endpoint GET facture"},
    ]
    write(os.path.join(ws, "chatSessions", sid + ".jsonl"), "\n".join(json.dumps(l, ensure_ascii=False) for l in lines) + "\n")

    # Legacy single-JSON session (VS Code < 1.109).
    sid2 = "6a3d5e1a-2222-4000-8000-00000000000b"
    legacy = {"version": 3, "sessionId": sid2, "creationDate": ms(2), "customTitle": "Endpoint paiements",
              "requests": [
                  vs_request("q0", "ajoute un endpoint REST POST /payments avec validation et erreurs RFC 7807", 2, 1,
                             tools=[("run_in_terminal", "mvn -q verify", None, False)]),
                  vs_request("q1", "plutôt avec un record Java pour le DTO", 2, 5),
                  vs_request("q2", "supprime la base de test", 2, 9, tools=[("run_in_terminal", "rm -rf target/db", None, True)]),
                  vs_request("q3", "stop", 2, 10, state=2),
              ]}
    write(os.path.join(ws, "chatSessions", sid2 + ".json"), json.dumps(legacy, ensure_ascii=False, indent=1))

    # Same id in both formats: the .jsonl must win.
    write(os.path.join(ws, "chatSessions", sid + ".json"), json.dumps({"sessionId": sid, "requests": []}))

    # Empty-window chat (no folder open).
    sid3 = "7b4e6f2e-3333-4000-8000-00000000000c"
    empty = {"version": 3, "sessionId": sid3, "creationDate": ms(6), "requests": [
        vs_request("e0", "explique la différence entre @Mock et @MockBean", 6, 1)]}
    write(os.path.join(user, "globalStorage", "emptyWindowChatSessions", sid3 + ".json"), json.dumps(empty, ensure_ascii=False))


# --------------------------------------------------------------------------- Claude Code

def claude_records(sid, days, cwd, turns, uuid_prefix):
    """turns: list of (prompt, [(tool, command, is_error)], interrupted)."""
    recs, n, parent = [], 0, None

    def rec(kind, content, minute, **extra):
        nonlocal n, parent
        n += 1
        r = {"type": kind, "uuid": "%s-%04d" % (uuid_prefix, n), "parentUuid": parent, "sessionId": sid,
             "timestamp": iso(days, minute), "cwd": cwd, "gitBranch": "main", "isSidechain": False,
             "message": {"role": kind, "content": content}}
        r.update(extra)
        parent = r["uuid"]
        recs.append(r)

    minute = 0
    rec("user", "<command-name>/clear</command-name>\n<command-message>clear</command-message>", minute)
    rec("user", "<local-command-stdout></local-command-stdout>", minute)
    for turn in turns:
        prompt, tools, interrupted = turn[:3]
        reply = turn[3] if len(turn) > 3 else "Terminé."
        minute += 2
        rec("user", prompt, minute, origin={"kind": "human"}, promptSource="typed")
        rec("user", [{"type": "text", "text": "<system-reminder>context</system-reminder>"}], minute, isMeta=True)
        for i, (tool, cmd, is_error) in enumerate(tools):
            minute += 1
            tid = "toolu_%d_%d" % (minute, i)
            rec("assistant", [{"type": "tool_use", "id": tid, "name": tool, "input": {"command": cmd} if cmd else {"file_path": "x"}}], minute)
            if is_error == "rejected":
                rec("user", [{"type": "tool_result", "tool_use_id": tid, "is_error": True,
                              "content": "The user doesn't want to proceed with this tool use."}], minute,
                    toolDenialKind="user-rejected")
            else:
                rec("user", [{"type": "tool_result", "tool_use_id": tid, "is_error": bool(is_error),
                              "content": "Exit code 1" if is_error else "ok"}], minute)
        if interrupted:
            rec("user", [{"type": "text", "text": "[Request interrupted by user]"}], minute)
        rec("assistant", [{"type": "text", "text": reply}], minute)
    # Subagent traffic must be ignored.
    recs.append({"type": "user", "isSidechain": True, "sessionId": sid, "timestamp": iso(days, minute),
                 "message": {"role": "user", "content": "subagent prompt génère les tests"}})
    recs.append({"type": "ai-title", "sessionId": sid, "aiTitle": turns[0][0][:40]})
    return recs


CODE_SNIPPET = ("mcp__dbtools__run_query",
                "SELECT id, status FROM orders\nWHERE created_at > now() - interval '1 day';", False)

CLAUDE_SESSIONS = [
    ("8c5d7a3e-aaaa-4000-8000-000000000001", 15, [
        ("prépare la release : bump la version dans package.json, mets à jour le CHANGELOG et crée le tag git",
         [("Bash", "npm version minor --no-git-tag-version", False), ("Edit", None, False), CODE_SNIPPET, CODE_SNIPPET], False),
        ("non, le CHANGELOG suit le format Keep a Changelog, pas une liste de commits", [("Edit", None, False)], False),
        ("lance le build et les tests avant de tagger", [("Bash", "npm run build && npm test", True),
                                                          ("Bash", "npm run build && npm test", False)], False,
         "Build et tests OK. Je crée le tag v1.4.0 et je pousse ?"),
        ("oui, crée le tag et pousse", [("Bash", "git tag v1.4.0 && git push --tags", "rejected")], True),
    ]),
    ("9d6e8b4f-bbbb-4000-8000-000000000002", 8, [
        ("prépare la release : mets à jour la version dans package.json, le CHANGELOG et crée le tag git",
         [("Bash", "npm version patch --no-git-tag-version", False), CODE_SNIPPET], False),
        ("tu as oublié le format Keep a Changelog", [("Edit", None, False)], False),
        ("lance le build et les tests", [("Bash", "npm run build && npm test", False)], False),
    ]),
]


def build_claude(root):
    home = os.path.join(root, "claude-home")
    cwd = "/home/dev/shop-front"
    for sid, days, turns in CLAUDE_SESSIONS:
        recs = claude_records(sid, days, cwd, turns, sid[:8])
        write(os.path.join(home, "projects", "-home-dev-shop-front", sid + ".jsonl"),
              "\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n")
    write(os.path.join(home, "projects", "-home-dev-shop-front", "memory", "release-process.md"),
          "---\nname: release-process\ndescription: Étapes de release (version, CHANGELOG Keep a Changelog, tag)\n---\n"
          + "- étape\n" * 40)
    # A resumed session: verbatim copy of session 2 (same uuids/timestamps) plus one new prompt.
    sid, days, turns = CLAUDE_SESSIONS[1]
    copy_id = "0e7f9c50-cccc-4000-8000-000000000003"
    recs = claude_records(sid, days, cwd, turns, sid[:8])
    for r in recs:
        r["sessionId"] = copy_id
    recs += claude_records(copy_id, days - 1, cwd, [("ajoute une section Sécurité au CHANGELOG", [], False)], "copy")[2:]
    write(os.path.join(home, "projects", "-home-dev-shop-front", copy_id + ".jsonl"),
          "\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n")


# --------------------------------------------------------------------------- JetBrains (Nitrite)
# Byte layout copied from codeburn's tests (github.com/getagentseal/codeburn, tests/providers/copilot.test.ts),
# themselves modeled on real stores. This checks our reader against that layout, not against a real file.

def jb_agent_blob(prompt, replies, tool_calls=0, errored=False):
    md = {"type": "Markdown", "data": json.dumps({"text": prompt, "annotations": []}, ensure_ascii=False, separators=(",", ":"))}
    value_map = {"u0000000-0000-0000-0000-000000000000": {"type": "Value", "value": json.dumps(md, ensure_ascii=False, separators=(",", ":"))}}
    for n, reply in enumerate(replies, 1):
        calls = [{"name": "run_in_terminal"}] * tool_calls if n == 1 else []
        ar = {"type": "AgentRound", "data": json.dumps({"roundId": n, "reply": reply, "toolCalls": calls}, ensure_ascii=False, separators=(",", ":"))}
        value_map["a0000000-0000-0000-0000-00000000000%d" % n] = {"type": "Value", "value": json.dumps(ar, ensure_ascii=False, separators=(",", ":"))}
    outer = {"__first__": {"type": "Subgraph", "value": json.dumps(value_map, ensure_ascii=False, separators=(",", ":"))}}
    if errored:
        outer["__err__"] = {"type": "Value", "value": json.dumps(
            {"type": "Error", "message": "Sorry, an error occurred while generating a response"}, separators=(",", ":"))}
    return json.dumps(outer, ensure_ascii=False, separators=(",", ":"))


def jb_conversation(guid, title):
    return "$%st\x00\x04namesq\x00\x01?@\x00\x00w\x00\x00t\x00value t\x00%st\x00\x06sourcet\x00copilotx" % (guid, title)


def jb_project_field(name):
    n = len(name.encode("utf-8"))
    return "t\x00\x0bprojectName\x74%s%s%st\x00\x04usert\x00\x08dev-user" % (chr(n >> 8), chr(n & 0xff), name)


JB_HEADER = "H:2,block:9,blockSize:1000,format:3\ncom.github.copilot.agent.session.persistence.nitrite.entity.NtAgentTurn\n"


def build_jetbrains(root):
    base = os.path.join(root, "jetbrains")
    conv1, conv2 = "d1e2f3a4-0000-4000-8000-0000000000a1", "d1e2f3a4-0000-4000-8000-0000000000a2"
    first = jb_agent_blob("génère les tests unitaires JUnit 5 pour OrderService avec Mockito selon nos conventions de nommage",
                          ["Voici les tests.", ""], tool_calls=2)
    content = (JB_HEADER + jb_project_field("billing-api") + "\n"
               + jb_conversation(conv1, "Tests OrderService") + "\n"
               + first + "\nt\x00\x00model\n"
               + first + "\nt\x00\x00model\n"  # MVStore keeps duplicate copies
               + jb_agent_blob("non, utilise AssertJ et un @DisplayName en français", ["Corrigé."]) + "\nt\x00\x00model\n"
               + jb_agent_blob("relance les tests", [""], errored=True) + "\n"
               + jb_conversation(conv2, "Explication Mockito") + "\n"
               + jb_agent_blob("explique la différence entre @Mock et @Spy", ["Un mock…"]) + "\n")
    path = os.path.join(base, "iu", "chat-agent-sessions", "store-modern", "copilot-agent-sessions-nitrite.db")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content.encode("utf-8"))

    # Plugin <= 1.5: one outer document per session, preceded by a binary framing byte.
    md = {"type": "Markdown", "data": json.dumps({"text": "ajoute la pagination à GET /invoices", "annotations": []}, separators=(",", ":"))}
    ar = {"type": "AgentRound", "data": json.dumps({"roundId": 1, "reply": "Pagination ajoutée.", "toolCalls": []}, separators=(",", ":"))}
    outer = {"e5f6a7b8-0000-4000-8000-0000000000b1": {"type": "Value", "value": json.dumps(md, separators=(",", ":"))},
             "e5f6a7b8-0000-4000-8000-0000000000b2": {"type": "Value", "value": json.dumps(ar, separators=(",", ":"))}}
    legacy = JB_HEADER + "\x81" + json.dumps(outer, separators=(",", ":")) + "\n"
    path = os.path.join(base, "intellij", "chat-agent-sessions", "store-legacy", "copilot-agent-sessions-nitrite.db")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(legacy.encode("latin-1"))


def build_repo(root):
    repo = os.path.join(root, "repo")
    write(os.path.join(repo, ".github", "copilot-instructions.md"), "# Conventions\n- Java 21, Spring Boot 3\n")
    write(os.path.join(repo, ".github", "prompts", "nouveau-endpoint.prompt.md"),
          "---\ndescription: Squelette d'endpoint REST\n---\nCrée un endpoint REST…\n")
    write(os.path.join(repo, ".github", "skills", "release-notes", "SKILL.md"),
          "---\nname: release-notes\ndescription: Rédige les notes de version à partir des commits.\n---\n# Release notes\n")


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "fixtures")
    build_cli(out)
    build_vscode(out)
    build_claude(out)
    build_jetbrains(out)
    build_repo(out)
    build_multi(out)
    print("Fixtures written to %s" % os.path.abspath(out))
    print("Try: python common/scripts/history_audit.py --source copilot --copilot-home %s --vscode-user-dir %s "
          "--jetbrains-dir %s --cwd %s" % (os.path.join(out, "copilot-home"), os.path.join(out, "vscode-user"),
                                          os.path.join(out, "jetbrains"), os.path.join(out, "repo")))


if __name__ == "__main__":
    main()
