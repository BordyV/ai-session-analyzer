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
Par défaut : tout l'historique disponible et tous les projets. Si l'utilisateur donne une période (« ce mois-ci », « mes 10 derniers jours »), traduis-la en `--days N`. Ne pose une question que si la demande est vraiment ambiguë.

Distingue deux types d'audit, et annonce le type choisi en tête du rapport :
- **Audit global** : c'est le cas par défaut, et en particulier quand l'utilisateur dit « depuis le début », « en général » ou « sur tout mon historique ». Le rapport couvre tous les projets du digest avant toute recommandation propre à un projet.
- **Audit d'un projet** : seulement si l'utilisateur parle de « ce projet » ou en nomme un. Filtre alors avec `--project <nom du dossier>`.

Le dossier courant ne décide pas du périmètre. Un audit global lancé depuis un projet reste global. Le script y cherche seulement l'existant (`--cwd`, par défaut le dossier courant).

### 2. Produire le digest
Le script se trouve dans `scripts/history_audit.py`, à côté de ce fichier (en général `~/.claude/skills/claude-session-analyzer/`). Il n'utilise que la bibliothèque standard de Python 3.8+.

```bash
python "<dossier du skill>/scripts/history_audit.py" --source claude --out "<dossier temporaire>/digest.md"
```

- Sous Windows, utilise `python` ou `py` ; sous macOS et Linux, `python3`.
- Écris le digest dans le dossier temporaire de la session (scratchpad), pas dans le projet de l'utilisateur.
- Si le digest est vide, lance `--list-sources` pour voir ce qui existe.
- La session d'audit en cours fait partie de l'historique. Exclus-la avec `--exclude-session <id>` si tu connais son identifiant ; si l'audit tourne depuis un dossier dédié (dépôt de skills, dossier d'outillage), exclus tout ce dossier avec `--exclude-project <nom>`. Les totaux restent ainsi justes.
- Sans Python disponible : lis quelques fichiers `.jsonl` récents. Les demandes de l'utilisateur sont les lignes `"type":"user"` dont `message.content` est une chaîne. Signale que l'analyse est partielle.

### 3. Lire, puis vérifier sans tout relire
Le digest contient déjà de quoi juger :
- les épisodes de friction avec leur contexte (demande, fin de réponse, réaction) ;
- les garde-fous tapés par l'utilisateur (« sans coder », « ne fais pas le build », fautes de frappe comprises) ;
- les défauts qu'il a signalés après une livraison (« ça dépasse », « ne s'affiche pas ») ;
- les questions de confirmation posées par l'assistant (« je commite ? » suivi de « oui ») ;
- les « non » qui répondent à une proposition de l'assistant, comptés à part et non comme des corrections ;
- les outils les plus utilisés, leurs erreurs fréquentes et le code réécrit d'une session à l'autre ;
- le coût côté assistant : les erreurs d'outils regroupées par cause (mauvais dossier, interpréteur introuvable, syntaxe du shell, script qui échoue sans message…) ;
- les sessions actives en même temps sur le même projet, et les modifications qu'une session attribue à une autre ;
- les notes de mémoire des projets.

Lis-le, puis `references/methodology.md` (sections 2 à 4). Pour chaque épisode, nomme la cause racine (A à I) : c'est elle qui détermine le remède, pas le nombre de tours.

Ne te limite pas aux épisodes de friction : ils ne montrent que ce que l'utilisateur a dit. Les sections « Coût côté assistant » et « Sessions en parallèle » révèlent ce qu'il ne voit pas (causes H et I), et elles produisent souvent les recommandations les plus rentables. Cherche aussi le savoir métier découvert en cours de route (méthodologie, section 2).

Ensuite, vérifie au plus juste :
- **Une hypothèse** (« je demande souvent X ? », « Claude demande-t-il souvent Y ? ») : `--grep "motif"`, ou `--grep "motif" --grep-replies` pour chercher dans les réponses. Une commande donne les occurrences, les sessions, les jours et des exemples.
  - Ajoute toujours `--grep-log "<dossier temporaire>/greps.md"` : chaque recherche est conservée, avec son motif et ses exemples.
  - Lis les exemples avant de citer le chiffre. Un exemple hors sujet (« prendre en compte » pour un compte de test, « le like sur tiktok » pour une publication) veut dire que le motif est trop large : resserre-le (mots plus précis, noms propres, verbes d'action) et relance. Ne cite que le chiffre du motif final, qui ne ramène plus de faux positif.
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
Suis `references/report-template.md`. Pour chaque skill recommandé, fournis un brouillon de `SKILL.md` dont la `description` reprend les formulations réelles relevées dans l'historique. Chaque recommandation, y compris un script ou un outil, donne un emplacement de fichier et un gain chiffré.

Avant de rendre le rapport, relis-le avec la section 6 de la méthodologie :
- un même chiffre est identique dans toutes les sections, avec un périmètre annoncé. Préfère les chiffres du digest ; pour un chiffre tiré de `--grep`, cite le motif ;
- le « En bref » ne dit pas plus que le corps : des ratios, pas « toujours » ni « à chaque fois » ; une hypothèse reste présentée comme une hypothèse ; le nombre de recommandations et de thèmes écartés correspond aux sections ;
- un thème n'est classé « sous le seuil » qu'après un `--grep` de vérification ;
- les limites décrivent la méthode réellement suivie (sessions ouvertes, recherches).

Respecte la confidentialité : courts extraits uniquement, aucun secret, et les sessions personnelles (santé, juridique…) résumées en une catégorie, sans citation.

Termine en proposant de mettre en œuvre la recommandation n° 1. Si le plugin `skill-creator` est installé, propose-le pour créer et tester le skill. N'écris rien dans `~/.claude/`, dans un `CLAUDE.md` ou dans `settings.json` sans l'accord explicite de l'utilisateur : ce sont des fichiers qui modifient le comportement de toutes ses sessions.

## Options utiles du script

| Option | Effet |
|---|---|
| `--days N` | Sessions actives dans les N derniers jours (`0` = tout) |
| `--project texte` | Filtre sur le nom ou le chemin du projet |
| `--exclude-project texte` | Ignore les sessions dont le nom de projet ou le chemin contient ce texte, ex. `skills` pour `D:\dev\claude\skills` (pas le nom encodé du dossier de transcripts, `D--dev-claude-skills`) |
| `--grep "motif"` | Compte et illustre les demandes qui correspondent (`--grep-replies` : dans les réponses) |
| `--show-session id` | Vue compacte d'une session (`--full` : tous les tours) |
| `--format json` | Sortie structurée (pour un traitement complémentaire) |
| `--similarity 0.35` | Groupes de demandes plus larges (0.4 par défaut ; monter à 0.5 pour des groupes plus stricts) |
| `--min-sessions 3` | N'affiche que les demandes vues dans au moins 3 sessions |
| `--source all` | Ajoute l'historique GitHub Copilot s'il existe sur la machine |
