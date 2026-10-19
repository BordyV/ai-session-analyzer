#!/usr/bin/env python3
"""Regression tests for common/scripts/history_audit.py.

    python tests/test_history_audit.py
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "common", "scripts"))
sys.path.insert(0, HERE)

import history_audit as ha  # noqa: E402
import make_fixtures  # noqa: E402

FIXTURES = tempfile.mkdtemp(prefix="history_audit_fixtures_")
make_fixtures.build_cli(FIXTURES)
make_fixtures.build_vscode(FIXTURES)
make_fixtures.build_claude(FIXTURES)
make_fixtures.build_jetbrains(FIXTURES)
make_fixtures.build_repo(FIXTURES)

COMMON = ["--copilot-home", os.path.join(FIXTURES, "copilot-home"),
          "--vscode-user-dir", os.path.join(FIXTURES, "vscode-user"),
          "--claude-home", os.path.join(FIXTURES, "claude-home"),
          "--jetbrains-dir", os.path.join(FIXTURES, "jetbrains"),
          "--cwd", os.path.join(FIXTURES, "repo")]


def run(*argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = ha.main(list(argv) + COMMON)
    return code, buf.getvalue()


def digest(source):
    code, out = run("--source", source, "--format", "json")
    assert code == 0, out
    return json.loads(out)


def by_prefix(sessions, prefix):
    return next(s for s in sessions if s.id.startswith(prefix))


class CopilotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = digest("copilot")
        args = ha.argparse.Namespace(copilot_home=COMMON[1], vscode_user_dir=COMMON[3], claude_home=COMMON[5],
                                     jetbrains_dir=COMMON[7])
        cls.sessions = ha.load_sessions(ha.COPILOT_SOURCES, args)
        idf, _df, _n = ha.compute_idf(cls.sessions)
        for s in cls.sessions:
            ha.session_metrics(s, idf)

    def test_sources_counted_once(self):
        self.assertEqual(self.d["overview"]["by_source"], {"copilot-cli": 4, "vscode": 3, "jetbrains": 4})

    def test_session_store_fallback(self):
        s = by_prefix(self.sessions, "4e1b3cf2")
        self.assertEqual(len(s.turns), 2)
        self.assertEqual(s.metrics["corrections"], 1)

    def test_mutation_log_replay(self):
        s = by_prefix(self.sessions, "5f2c4d06")
        self.assertEqual(s.title, "Endpoint GET facture")
        self.assertEqual([t.text[:12] for t in s.turns], ["ajoute un en", "ça ne marche", "toujours pas"])
        self.assertTrue(s.turns[1].downvote)
        self.assertEqual(s.metrics["tool_errors"], 2)
        self.assertEqual(s.turns[0].shell, ["mvn -q verify"])

    def test_intellij_agent_session(self):
        s = by_prefix(self.sessions, "8f0e1d2c")
        self.assertEqual((s.source, s.client, s.title), ("jetbrains", "copilot-intellij", "Revue de la sécurité"))
        self.assertEqual(len(s.turns), 3)  # the sub-agent prompt is not the user's
        self.assertEqual(s.turns[0].reply, "Deux risques moyens relevés. Je lance les tests de sécurité ?")
        self.assertEqual((s.turns[0].rejections, s.turns[0].tool_errors), (1, 0))  # denied once, not also an error
        self.assertEqual(s.turns[1].tool_errors, 1)
        self.assertIn("GARDE_FOU", s.turns[0].flags)  # "sans cdore"
        self.assertIn("DEFAUT_SIGNALE", s.turns[2].flags)
        self.assertEqual(self.d["assistant_questions"]["action"]["answered_yes"], 1)
        self.assertIn("Failures", self.d["tool_errors"][0]["example"] + " ".join(r["example"] for r in self.d["tool_errors"]))

    def test_source_filter_keeps_ide_sessions(self):
        d = digest("jetbrains")
        self.assertEqual(d["overview"]["by_client"], {"copilot-intellij": 1, "jetbrains-nitrite": 3})

    def test_replies_are_captured(self):
        self.assertEqual(by_prefix(self.sessions, "5f2c4d06").turns[0].reply, "Voici la proposition.")
        self.assertEqual(by_prefix(self.sessions, "1b7e44c0").turns[0].reply, "Fait.")

    def test_legacy_json_interrupt_and_rejection(self):
        s = by_prefix(self.sessions, "6a3d5e1a")
        self.assertEqual(s.metrics["interruptions"], 1)
        self.assertEqual(s.metrics["tool_rejections"], 1)

    def test_recurring_request_found(self):
        top = self.d["recurring_requests"][0]
        self.assertEqual(top["sessions"], 5)  # "génère" and "écris les tests" are the same task
        self.assertTrue(all("JUnit 5" in e["text"] for e in top["examples"]))
        self.assertEqual(top["sources"], ["copilot-cli", "jetbrains"])  # same task across tools

    def test_recurring_shell_command(self):
        cmds = {r["command"] for r in self.d["recurring_shell_commands"]}
        self.assertIn("mvn test", cmds)

    def test_existing_automation_detected(self):
        self.assertEqual([s["name"] for s in self.d["existing_skills"]], ["release-notes"])
        self.assertEqual([c["name"] for c in self.d["existing_commands"]], ["/nouveau-endpoint"])
        self.assertTrue(any(p.endswith("copilot-instructions.md") for p in self.d["instruction_files"]))

    def test_missing_project_dirs_are_flagged(self):
        # The fixtures record /home/dev/billing-api: the repo is only found through --cwd, and the digest says so.
        self.assertTrue(any("--cwd" in w and "billing-api" in w for w in self.d["warnings"]))

    def test_project_filter_lists_unscoped_sessions(self):
        code, out = run("--source", "copilot", "--project", "billing-api", "--format", "json")
        self.assertEqual(code, 0)
        un = json.loads(out)["unscoped"]
        firsts = " ".join(" ".join(x["first"]) for x in un["sessions"])
        self.assertEqual(un["count"], 2)  # VS Code empty window + old JetBrains store, neither tied to a folder
        self.assertIn("pagination", firsts)
        self.assertIn("@MockBean", firsts)

    def test_unscoped_listed_without_project_filter(self):
        un = self.d["unscoped"]
        self.assertFalse(self.d["unscoped_filtered"])
        self.assertIn("@MockBean", " ".join(" ".join(x["first"]) for x in un["sessions"]))

    def test_unverified_existing_is_said(self):
        argv = ["--source", "copilot"] + COMMON[:-1] + [os.path.join(FIXTURES, "vscode-user")]  # --cwd without the repo
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(ha.main(argv), 0)
        out = buf.getvalue()  # nothing found, and the digest says it could not look where the repo was
        self.assertIn("non vérifié dans les dépôts introuvables", out)

    def test_tool_error_causes(self):
        causes = {r["cause"]: r for r in self.d["tool_error_causes"]}
        self.assertIn("mvn test", dict(causes["tests"]["commands"]))
        self.assertEqual(causes["muet"]["count"], 2)  # VS Code "Command failed: exit code 1" on mvn verify


class JetBrainsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sessions = [s for s in ha.load_jetbrains(COMMON[7]) if s.turns]
        idf, _df, _n = ha.compute_idf(cls.sessions)
        for s in cls.sessions:
            ha.session_metrics(s, idf)

    def test_conversations_and_titles(self):
        titles = sorted(s.title or "" for s in self.sessions)
        self.assertEqual(titles, ["", "Explication Mockito", "Tests OrderService"])

    def test_agent_prompts_deduplicated_and_decoded(self):
        s = next(s for s in self.sessions if s.title == "Tests OrderService")
        self.assertEqual(s.project, "billing-api")
        self.assertEqual(len(s.turns), 3)
        self.assertIn("génère les tests", s.turns[0].text)
        self.assertEqual(s.turns[0].tools["agent-tool"], 2)
        self.assertEqual(s.metrics["corrections"], 1)
        self.assertTrue(s.turns[2].failed)

    def test_legacy_format(self):
        s = next(s for s in self.sessions if not s.title)
        self.assertEqual([t.text for t in s.turns], ["ajoute la pagination à GET /invoices"])

    def test_diagnose(self):
        code, out = run("--jetbrains-diagnose")
        self.assertEqual(code, 0)
        self.assertIn("NtAgentTurn", out)
        self.assertIn("AgentRound", out)
        self.assertIn("projectName : billing-api", out)
        code, out = run("--jetbrains-diagnose", "--no-excerpts")
        self.assertNotIn("OrderService avec", out)


class ClaudeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = digest("claude")
        args = ha.argparse.Namespace(claude_home=COMMON[5])
        cls.sessions = ha.load_sessions({"claude"}, args)
        idf, _df, _n = ha.compute_idf(cls.sessions)
        for s in cls.sessions:
            ha.session_metrics(s, idf)

    def test_resumed_copy_is_deduplicated(self):
        # The resumed session holds the whole story; the parent is fully contained in it and disappears.
        copy = by_prefix(self.sessions, "0e7f9c50")
        self.assertEqual(len(copy.turns), 4)
        self.assertEqual(copy.turns[-1].text, "ajoute une section Sécurité au CHANGELOG")
        self.assertEqual(len(by_prefix(self.sessions, "9d6e8b4f").turns), 0)
        self.assertEqual(self.d["overview"]["sessions"], 2)

    def test_noise_is_filtered(self):
        s = by_prefix(self.sessions, "8c5d7a3e")
        self.assertEqual(len(s.turns), 4)  # /clear, stdout, meta and sidechain records ignored
        self.assertEqual(s.commands["clear"], 1)

    def test_friction_signals(self):
        s = by_prefix(self.sessions, "8c5d7a3e")
        self.assertEqual(s.metrics["corrections"], 1)
        self.assertEqual(s.metrics["tool_errors"], 1)
        self.assertEqual(s.metrics["tool_rejections"], 1)
        self.assertEqual(s.metrics["interruptions"], 1)

    def test_release_flow_recurs(self):
        top = self.d["recurring_requests"][0]
        self.assertEqual(top["sessions"], 2)
        self.assertTrue(all("release" in e["text"] for e in top["examples"]))
        self.assertIn("release", top["terms"])

    def test_show_session(self):
        code, out = run("--source", "claude", "--show-session", "8c5d")
        self.assertEqual(code, 0)
        self.assertIn("[CORRECTION]", out)
        self.assertIn("INTERRUPTION", out)
        self.assertIn("vue compacte", out)
        self.assertIn("fin de réponse", out)

    def test_assistant_questions(self):
        q = self.d["assistant_questions"]["git"]
        self.assertEqual((q["questions"], q["answered_yes"]), (1, 1))

    def test_episodes_have_context(self):
        ep = next(e for e in self.d["friction_episodes"] if "CORRECTION" in e["kinds"])
        self.assertIn("prépare la release", ep["asked"])
        self.assertIn("Keep a Changelog", ep["reaction"])

    def test_repeated_tool_input(self):
        top = self.d["tool_inputs"]["repeated"][0]
        self.assertEqual((top["tool"], top["calls"], top["sessions"]), ("mcp__dbtools__run_query", 3, 2))
        self.assertIn("mcp:dbtools", [r["tool"] for r in self.d["tool_usage"]])

    def test_memories(self):
        self.assertEqual(self.d["memories"][0]["file"], "release-process.md")
        self.assertIn("Keep a Changelog", self.d["memories"][0]["summary"])

    def test_grep(self):
        _code, out = run("--source", "claude", "--grep", "release")
        self.assertIn("2 occurrences · 2 sessions", out)
        _code, out = run("--source", "claude", "--grep", "je pousse", "--grep-replies")
        self.assertIn("1 occurrences", out)
        self.assertIn("réponses de l'assistant", out)

    def test_grep_log_keeps_every_search(self):
        log = os.path.join(tempfile.mkdtemp(), "work", "greps.md")
        run("--source", "claude", "--grep", "release", "--grep-log", log)
        run("--source", "claude", "--grep", "changelog", "--grep-log", log)
        with open(log, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("Motif « release »", text)
        self.assertIn("Motif « changelog »", text)


class DistTests(unittest.TestCase):
    """dist/ is committed so users can copy a skill as is: it must match the sources."""

    def test_dist_is_up_to_date(self):
        root = os.path.join(HERE, "..")
        for platform, name in (("claude", "claude-session-analyzer"), ("copilot", "copilot-session-analyzer")):
            expected = {"SKILL.md": os.path.join(root, platform, "SKILL.md")}
            for sub in ("scripts", "references"):
                for f in os.listdir(os.path.join(root, "common", sub)):
                    if not f.endswith(".pyc") and f != "__pycache__":
                        expected["%s/%s" % (sub, f)] = os.path.join(root, "common", sub, f)
            for rel, src in expected.items():
                built = os.path.join(root, "dist", name, rel)
                with open(src, "rb") as a, open(built, "rb") as b:
                    self.assertEqual(a.read().replace(b"\r\n", b"\n"), b.read().replace(b"\r\n", b"\n"),
                                     "dist/%s/%s est périmé : lance `python build.py`" % (name, rel))


class HelperTests(unittest.TestCase):
    def test_command_keys(self):
        keys = lambda c: [k for k, _ in ha.command_keys(c)]  # noqa: E731
        self.assertEqual(keys('cd /x && git add -A; git commit -m "fix: a|b"'), ["git add", "git commit"])
        self.assertEqual(keys("python - <<'EOF'\nimport os\nEOF"), [])
        self.assertEqual(keys('& "C:\\My Tools\\deploy.ps1" -Port 8080'), ["deploy.ps1"])
        self.assertEqual(keys("node -e 'const a = 1;\nconsole.log(a)'"), [])
        self.assertEqual(keys("mvn -q test -Dtest=Foo"), ["mvn test"])
        self.assertEqual(keys("git -C api status && git -C api push"), ["git push"])

    def test_declined_offer_is_not_a_correction(self):
        self.assertTrue(ha.declines_offer("Non pas de suite", "Je la développe sur ce modèle ?"))
        self.assertTrue(ha.declines_offer("non mais stop", "Je l'ajoute au test ?"))
        self.assertFalse(ha.declines_offer("non, utilise AssertJ", "Fait."))  # reacts to delivered work
        self.assertFalse(ha.declines_offer("non je t'ai dit AssertJ", "Je lance les tests ?"))  # a real correction

    def test_corrections(self):
        self.assertTrue(ha.correction_label("non, utilise AssertJ"))
        self.assertTrue(ha.correction_label("ça ne marche toujours pas"))
        self.assertTrue(ha.correction_label("No, I said pnpm"))
        self.assertIsNone(ha.correction_label("ajoute un endpoint REST"))

    def test_typo_tolerance(self):
        self.assertTrue(ha.typo_equal("cdore", "coder"))
        self.assertTrue(ha.typo_equal("biuld", "build"))
        self.assertTrue(ha.typo_equal("commti", "commit"))
        self.assertFalse(ha.typo_equal("change", "charge"))
        self.assertFalse(ha.typo_equal("recession", "regression"))
        self.assertFalse(ha.typo_equal("de", "le"))

    def test_guardrails(self):
        cats = lambda t: [c for c, _p, _o in ha.GUARDRAIL_MATCHER.find(t)]  # noqa: E731
        self.assertEqual(cats("sans cdore tu en penses quoi ?"), ["ne rien coder / modifier"])
        self.assertEqual(cats("ne fait pas le biuld tout de suite"), ["pas de build / commit / push / déploiement"])
        self.assertEqual(cats("ne touche strictement à rien"), ["ne rien coder / modifier"])
        self.assertEqual(cats("lance les tests, oublie pas de push"), [])
        self.assertEqual(cats("Mode lecture seule pour les comptes expirés"), [])
        self.assertEqual(cats("à quoi dois-je m'attendre demain ?"), [])

    def test_defects(self):
        found = lambda t: bool(ha.DEFECT_MATCHER.find(t))  # noqa: E731
        self.assertTrue(found("attention le bouton dépasse de la carte"))
        self.assertTrue(found("ça s'affiche pas"))
        self.assertFalse(found("en cas de récession du marché"))
        self.assertFalse(found("ça change pas"))

    def test_error_causes(self):
        cause = lambda tool, text: ha.error_cause(tool, text)[0]  # noqa: E731
        self.assertEqual(cause("Bash", "Exit code 128\nfatal: not a git repository (or any of the parent directories)"), "dossier")
        self.assertEqual(cause("Bash", "Exit code 127\n/usr/bin/bash: line 1: python: command not found"), "environnement")
        self.assertEqual(cause("Bash", "Exit code 2\nbash: -c: line 3: unexpected EOF while looking for matching `''"), "syntaxe")
        self.assertEqual(cause("Edit", "<tool_use_error>String to replace not found in file."), "edition")
        self.assertEqual(cause("Grep", "Ripgrep search timed out after 20 seconds."), "delai")
        self.assertEqual(cause("PowerShell", "Exit code 1"), "muet")
        self.assertEqual(cause("PowerShell", "Exit code 1\nLines\nDone\n[ERROR] Tests run: 5, Failures: 1"), "tests")
        self.assertEqual(cause("mcp__claude-in-chrome__computer", "Element is detached"), "externe")

    def test_foreign_changes(self):
        s = ha.Session("claude", "x", "x")
        ha.note_foreign(s, None, "assistant", "`logo.py` contient des modifications qui ne viennent pas de moi.")
        ha.note_foreign(s, None, "assistant", "C'est toi qui les as écrits, ou une autre session ?")
        ha.note_foreign(s, None, "assistant", "**Ce que je n'ai pas fait** : relancer les tests.")
        ha.note_foreign(s, None, "utilisateur", "on verra ça dans une autre session")
        self.assertEqual(len(s.foreign), 2)

    def test_overlaps(self):
        def session(sid, project, start_min, minutes):
            s = ha.Session("claude", sid, sid)
            s.project = s.cwd = project
            t0 = ha.NOW - ha.dt.timedelta(days=1) + ha.dt.timedelta(minutes=start_min)
            for m in range(0, minutes + 1, 5):
                s.turns.append(ha.Turn(t0 + ha.dt.timedelta(minutes=m), "x"))
                s.touch(t0 + ha.dt.timedelta(minutes=m))
            return s
        a, b = session("a", "/p", 0, 60), session("b", "/p", 30, 60)
        c, d = session("c", "/other", 0, 60), session("d", "/p", 200, 30)  # other project / later the same day
        ov = ha.overlap_report([a, b, c, d])
        self.assertEqual((ov["pairs"], ov["sessions"], ov["peak"]), (1, 2, 2))
        self.assertEqual(ov["examples"][0]["minutes"], 32)

    def test_redaction(self):
        self.assertNotIn("ghp_", ha.excerpt("token ghp_abcdefghijklmnopqrstuvwxyz0123", 200))
        self.assertIn("<email>", ha.excerpt("écris à jean.dupont@example.com", 200))


if __name__ == "__main__":
    unittest.main(verbosity=2)
