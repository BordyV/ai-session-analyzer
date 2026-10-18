---
name: claude-session-analyzer
description: Audite l'historique local des conversations Claude Code (~/.claude/projects) pour repérer les tâches répétitives, les demandes qui reviennent d'une session à l'autre et les sessions où il a fallu plusieurs tours de corrections, puis recommande, chiffres à l'appui, ce qui mérite un skill, une règle CLAUDE.md, une commande, un hook… ou rien. À utiliser dès que l'utilisateur veut analyser, auditer ou retracer son historique Claude, savoir ce qu'il redemande souvent, où il perd du temps ou tourne en rond, quels skills créer, ou faire le bilan de ses sessions, même sans dire « skill » ni « historique » (« qu'est-ce que je demande tout le temps à Claude ? », « où est-ce que je fais des allers-retours ? », « what do I keep asking Claude to do? »).
---

# Analyse de l'historique Claude Code

Ton rôle : répondre, preuves à l'appui, à la question **« qu'est-ce qui mérite d'être automatisé dans ma façon de travailler avec Claude, et qu'est-ce qui ne le mérite pas ? »**

Le partage du travail est simple : le script compte (tours, corrections, demandes récurrentes, commandes répétées), toi tu juges (cause racine, décision, rapport). Tout reste en local, aucune donnée n'est envoyée.

## Où se trouve l'historique

- Un fichier par session : `~/.claude/projects/<chemin-du-projet-encodé>/<session-id>.jsonl` (ou `$CLAUDE_CONFIG_DIR/projects/`).
- Claude Code purge les transcripts inactifs depuis plus de `cleanupPeriodDays` jours (30 par défaut, réglable dans `settings.json`). L'historique couvre donc souvent un mois au plus. Si l'utilisateur veut des analyses sur une période plus longue à l'avenir, suggère-lui d'augmenter ce réglage.

## Déroulé

### 1. Cadrer, sans sur-questionner
Par défaut : les 30 derniers jours et tous les projets. Si l'utilisateur parle de « ce projet », filtre avec `--project <nom du dossier courant>`. Ne pose une question que si la demande est vraiment ambiguë.

### 2. Produire le digest
Le script se trouve dans `scripts/history_audit.py`, à côté de ce fichier (en général `~/.claude/skills/claude-session-analyzer/`). Il n'utilise que la bibliothèque standard de Python 3.8+.

```bash
python "<dossier du skill>/scripts/history_audit.py" --source claude --days 30 --out "<dossier temporaire>/digest.md"
```

- Sous Windows, utilise `python` ou `py` ; sous macOS et Linux, `python3`.
- Écris le digest dans le dossier temporaire de la session (scratchpad), pas dans le projet de l'utilisateur.
- Si le digest est vide, lance `--list-sources` pour voir ce qui existe, puis élargis `--days 0` (tout l'historique disponible).
- La session d'audit en cours fait partie de l'historique. Exclus-la avec `--exclude-session <id>` si tu connais son identifiant, sinon ignore-la dans ta lecture.
- Sans Python disponible : lis quelques fichiers `.jsonl` récents. Les demandes de l'utilisateur sont les lignes `"type":"user"` dont `message.content` est une chaîne. Signale que l'analyse est partielle.

### 3. Lire, puis vérifier sans tout relire
Le digest contient déjà de quoi juger :
- les épisodes de friction avec leur contexte (demande, fin de réponse, réaction) ;
- les garde-fous tapés par l'utilisateur (« sans coder », « ne fais pas le build », fautes de frappe comprises) ;
- les défauts qu'il a signalés après une livraison (« ça dépasse », « ne s'affiche pas ») ;
- les questions de confirmation posées par l'assistant (« je commite ? » suivi de « oui ») ;
- les outils les plus utilisés, leurs erreurs fréquentes et le code réécrit d'une session à l'autre ;
- les notes de mémoire des projets.

Lis-le, puis `references/methodology.md` (sections 2 à 4). Pour chaque épisode, nomme la cause racine (A à G) : c'est elle qui détermine le remède, pas le nombre de tours.

Ensuite, vérifie au plus juste :
- **Une hypothèse** (« je demande souvent X ? », « Claude demande-t-il souvent Y ? ») : `--grep "motif"`, ou `--grep "motif" --grep-replies` pour chercher dans les réponses. Une commande donne les occurrences, les sessions, les jours et des exemples.
- **Un épisode ambigu** : `--show-session <id>` affiche une vue compacte (tours signalés et leur contexte, environ 800 tokens). Ouvre **5 sessions au maximum**, et seulement si le digest ne permet pas de nommer la cause. Réserve `--full` aux cas exceptionnels.
- **N'écris pas tes propres scripts d'extraction et ne lis pas les `.jsonl` directement.** Si une information manque au digest, signale-le dans les limites du rapport : c'est ce qui permettra d'améliorer le script.

Pourquoi ce budget : le digest coûte environ 5 000 tokens. Relire 20 sessions en coûte 20 000 à 50 000, pour très peu d'information en plus : la plupart des causes se lisent dans les épisodes. L'audit doit rester moins cher que le temps qu'il fait gagner.

Croise enfin avec la section « Déjà en place » : les commandes `.claude/commands/`, les skills et les `CLAUDE.md` existants. Si un outil couvre déjà le besoin, recommande de l'améliorer ou de le faire connaître, jamais de le dupliquer.

### 4. Décider
Applique la matrice de `references/methodology.md`, puis traduis chaque mécanisme générique en mécanisme Claude Code :

| Mécanisme générique | Dans Claude Code | Emplacement |
|---|---|---|
| Instruction persistante | Règle dans un `CLAUDE.md` | `~/.claude/CLAUDE.md` (toutes tes sessions), `<projet>/CLAUDE.md` (partagé avec l'équipe), `<projet>/CLAUDE.local.md` (perso, non versionné) |
| Instruction ciblée | `CLAUDE.md` dans le sous-dossier concerné | `<projet>/<sous-dossier>/CLAUDE.md` (chargé quand Claude travaille dans ce dossier) |
| Skill | Skill (également invocable par `/nom`) | `~/.claude/skills/<nom>/SKILL.md` (perso) ou `<projet>/.claude/skills/<nom>/SKILL.md` (équipe) |
| Prompt réutilisable | Commande personnalisée | `<projet>/.claude/commands/<nom>.md` ou `~/.claude/commands/<nom>.md`. Un skill invoqué par `/nom` remplit le même rôle avec plus de structure. |
| Automatisation déclenchée | Hook | `settings.json` → `hooks` (`PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `Stop`…). C'est le bon choix quand une action doit se produire à chaque fois : le harnais l'exécute, sans dépendre de la mémoire de Claude. |
| Agent personnalisé | Subagent | `<projet>/.claude/agents/<nom>.md` ou `~/.claude/agents/` |
| Accès externe | Serveur MCP | `claude mcp add …` ou `<projet>/.mcp.json` |

Pour distinguer règle et skill : si la correction tient en une phrase (« utilise pnpm », « les fichiers générés vont dans build/ »), c'est une règle `CLAUDE.md`. S'il faut une procédure, des exemples ou un script, c'est un skill.

### 5. Rendre le rapport
Suis `references/report-template.md`. Pour chaque skill recommandé, fournis un brouillon de `SKILL.md` dont la `description` reprend les formulations réelles relevées dans l'historique.

Respecte la confidentialité : courts extraits uniquement, aucun secret, et les sessions personnelles (santé, juridique…) résumées en une catégorie, sans citation.

Termine en proposant de mettre en œuvre la recommandation n° 1. Si le plugin `skill-creator` est installé, propose-le pour créer et tester le skill. N'écris rien dans `~/.claude/`, dans un `CLAUDE.md` ou dans `settings.json` sans l'accord explicite de l'utilisateur : ce sont des fichiers qui modifient le comportement de toutes ses sessions.

## Options utiles du script

| Option | Effet |
|---|---|
| `--days N` | Sessions actives dans les N derniers jours (`0` = tout) |
| `--project texte` | Filtre sur le nom ou le chemin du projet |
| `--grep "motif"` | Compte et illustre les demandes qui correspondent (`--grep-replies` : dans les réponses) |
| `--show-session id` | Vue compacte d'une session (`--full` : tous les tours) |
| `--format json` | Sortie structurée (pour un traitement complémentaire) |
| `--similarity 0.35` | Groupes de demandes plus larges (0.4 par défaut ; monter à 0.5 pour des groupes plus stricts) |
| `--min-sessions 3` | N'affiche que les demandes vues dans au moins 3 sessions |
| `--source all` | Ajoute l'historique GitHub Copilot s'il existe sur la machine |
