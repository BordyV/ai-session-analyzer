# Analyse d'historique : Claude Code et GitHub Copilot

Deux skills qui lisent l'historique local de tes conversations pour répondre à une question : **qu'est-ce qui mérite d'être automatisé (skill, règle, commande, hook…) et qu'est-ce qui ne le mérite pas ?**

Ils repèrent :
- les **tâches répétitives** : mêmes demandes d'une session à l'autre, mêmes suites de commandes ;
- les **tours** : sessions où il a fallu corriger, reformuler, interrompre ou refuser des actions ;
- ce qui est **déjà en place** : skills, commandes, fichiers d'instructions.

Ils rendent ensuite un rapport chiffré, avec des brouillons prêts à l'emploi. Tout reste en local, rien n'est envoyé.

## Organisation : un maximum en commun

```
common/                       ← partagé, environ 95 % du code
  scripts/history_audit.py    parseurs Claude Code + Copilot CLI + VS Code, métriques, regroupements
  references/methodology.md   causes des tours (A–G), matrice de décision, pièges
  references/report-template.md
claude/SKILL.md               ← spécifique : où est l'historique, mécanismes Claude Code
copilot/SKILL.md              ← spécifique : où est l'historique, mécanismes Copilot, /chronicle
claude/evals/, copilot/evals/ cas de test pour skill-creator
tests/                        données fictives + tests de non-régression du script
build.py                      assemble dist/<skill>/ (SKILL.md + common/), valide, installe
dist/                         généré par build.py et versionné : c'est ce qu'on distribue (ne pas modifier à la main)
```

Un skill installé doit être autonome. `build.py` recopie donc `common/` dans chacun d'eux. Corrige toujours les fichiers sources (`common/`, `claude/`, `copilot/`), puis relance la construction.

| | Claude Code | GitHub Copilot |
|---|---|---|
| Skill | `claude-session-analyzer` | `copilot-session-analyzer` |
| Historique lu | `~/.claude/projects/*/*.jsonl` | `~/.copilot/session-state/` (+ `session-store.db`), VS Code `workspaceStorage/*/chatSessions/`, JetBrains `github-copilot/<ide>/chat-*-sessions/*nitrite.db` (best-effort) |
| Recommande | `CLAUDE.md`, skills, `.claude/commands`, hooks `settings.json`, subagents, MCP | `copilot-instructions.md`, `*.instructions.md`, `.github/skills`, prompt files, `.github/hooks`, custom agents, MCP |
| Installation perso | `~/.claude/skills/` | `~/.copilot/skills/` |
| Installation équipe | `<repo>/.claude/skills/` | `<repo>/.github/skills/` |

Les noms sont distincts parce que Copilot lit aussi `~/.claude/skills/`. Si les deux skills sont installés, Copilot les voit tous les deux et choisit selon la description.

## Construire et installer

Sans rien construire : copie `dist/claude-session-analyzer/` dans `~/.claude/skills/` (ou `.claude/skills/` d'un projet), et `dist/copilot-session-analyzer/` dans `~/.copilot/skills/` (ou `.github/skills/` d'un dépôt). L'outil détecte le skill tout seul.

Après une modification des sources, relance `python build.py` : un test vérifie que `dist/` est à jour.

```bash
python build.py                          # construit et valide dist/
python build.py --install                # + ~/.claude/skills et ~/.copilot/skills
python build.py --install-repo ../mon-depot  # + .claude/skills et .github/skills du dépôt
python build.py --only copilot --install # une seule plateforme
```

## Utiliser

Dans Claude Code ou Copilot (mode agent ou CLI), demande simplement :
- « analyse mon historique du dernier mois et dis-moi quels skills je devrais créer »
- « où est-ce que je tourne en rond avec Claude sur mon projet principal ? »
- « qu'est-ce que je demande tout le temps à Copilot ? »

Le script s'utilise aussi seul :

```bash
python common/scripts/history_audit.py --list-sources
python common/scripts/history_audit.py --source claude --days 30
python common/scripts/history_audit.py --source copilot --show-session 5f2c      # vue compacte (--full : tout)
python common/scripts/history_audit.py --source claude --grep "commit|push"       # vérifier une hypothèse en une ligne
```

### Période analysée et rétention de l'historique

- `--days N` choisit la période : `--days 10` pour les 10 derniers jours, `--days 0` pour tout l'historique disponible. En langage naturel (« analyse mes 10 derniers jours »), le skill le traduit lui-même.
- **On ne remonte qu'aussi loin que l'historique conservé.** Claude Code supprime les transcripts inactifs depuis plus de `cleanupPeriodDays` jours (30 par défaut). Pour garder plus longtemps, ajoute dans `~/.claude/settings.json` :

  ```json
  { "cleanupPeriodDays": 180 }
  ```

  Ce réglage ne vaut que pour l'avenir : les transcripts déjà supprimés ne reviennent pas.
- `~/.claude/history.jsonl` (tes messages seuls, sans réponses ni outils) remonte souvent plus loin. Il n'est pas encore lu par le script.
- Copilot : la rétention dépend de l'outil (CLI, VS Code, IntelliJ). Elle n'est pas documentée officiellement.

### Coût d'un audit

Le digest (environ 5 000 tokens) contient les épisodes de friction avec leur contexte, les questions de confirmation de l'assistant, les outils et le code réécrits, et les notes de mémoire : de quoi conclure sans relire les sessions. Les skills limitent la lecture détaillée à 5 sessions en vue compacte (environ 800 tokens chacune) et interdisent les scripts d'extraction maison. La première version laissait les agents relire 15 à 22 sessions complètes (environ 49 000 tokens).

## Tester

```bash
python tests/test_history_audit.py       # 30 tests sur des historiques fictifs (Claude, Copilot CLI, VS Code, JetBrains)
python tests/make_fixtures.py tests/fixtures   # régénère les données fictives (dates relatives à aujourd'hui)
```

## IntelliJ / JetBrains : lecture best-effort

Le plugin Copilot de JetBrains range son chat natif dans des bases Nitrite : des fichiers H2 MVStore remplis d'objets Java sérialisés, un format non documenté. Le script les lit comme du texte, avec la méthode de [codeburn](https://github.com/getagentseal/codeburn). Il récupère ainsi les demandes du **mode Agent**, mais pas celles du mode Ask. Il n'a pas de date par tour, seulement la date du fichier. Les chats que le plugin fait passer par Copilot CLI (`copilot --acp`) sont dans `~/.copilot/session-store.db` et sont lus normalement.

Vérifié sur une vraie installation (IntelliJ 2025.2, plugin d'octobre 2026) : le plugin récent fait tourner ses conversations sur l'agent Copilot CLI. Le texte est dans `~/.copilot/session-state/<id>/` (`client_name: copilot-intellij`), lu et classé en source `jetbrains`, et la base Nitrite ne garde que des métadonnées. Elle est verrouillée tant qu'IntelliJ est ouvert. Le lecteur des anciennes bases, qui contenaient les conversations, a été écrit d'après la structure relevée par codeburn, **sans fichier réel de cet ancien format**. Pour le valider sur la machine d'un utilisateur d'IntelliJ :

```bash
python history_audit.py --list-sources                       # bases trouvées ?
python history_audit.py --jetbrains-diagnose --no-excerpts   # structure seule, aucun texte de conversation
python history_audit.py --source jetbrains --days 0          # ce que le skill verra
```

Si le diagnostic montre des « blobs de tour » mais 0 demande extraite, renvoie sa sortie : elle suffit pour adapter le lecteur à cette version du plugin.

## Ce qui a été vérifié par rapport à la discussion Gemini

- `/chronicle` existe bien dans Copilot CLI (`standup`, `tips`, `improve`, `search`, `cost-tips`, `reindex`), mais **uniquement en commande interactive**. `copilot chronicle standup --range 30d --format json` et `chronicle search … --count-only` n'existent pas. C'est pour cela que le skill lit directement les fichiers de session.
- `/chronicle standup` regroupe par statut et par branche, pas par « clusters conceptuels ». Le regroupement des demandes similaires est fait par `history_audit.py`.
- Les emplacements des skills Copilot (`.github/skills/`, `~/.copilot/skills/`, et aussi `.claude/skills/`, `.agents/skills/`) sont confirmés par la documentation Microsoft et GitHub.
- Le format des sessions VS Code (journal de mutations `kind` 0 à 3, depuis VS Code 1.109) a été vérifié dans le code source de VS Code (`objectMutationLog.ts`, `chatModel.ts`). Le format `events.jsonl` de Copilot CLI n'est pas documenté officiellement : le parseur est tolérant et retombe sur `session-store.db` si besoin.
- Les seuils fixes de Gemini (« plus de 15 à 20 requêtes par mois ») sont remplacés par des critères relatifs : fréquence, coût, stabilité, spécificité.
