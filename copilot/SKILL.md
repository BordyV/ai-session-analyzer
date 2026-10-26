---
name: copilot-session-analyzer
description: Audite l'historique local de GitHub Copilot, c'est-à-dire les sessions Copilot CLI (~/.copilot/session-state), Copilot Chat de VS Code (workspaceStorage/chatSessions) et du plugin JetBrains (IntelliJ, PyCharm…), pour repérer les tâches répétitives, les demandes récurrentes et les conversations où il a fallu plusieurs tours de corrections, puis recommande, chiffres à l'appui, ce qui mérite un skill (.github/skills), une instruction (copilot-instructions.md, *.instructions.md), un prompt file, un agent, un hook… ou rien. À utiliser dès que l'utilisateur veut analyser ou auditer son historique Copilot, savoir ce qu'il redemande souvent à Copilot, où il perd du temps, quels skills créer pour lui ou son équipe, ou aller plus loin que /chronicle, même sans dire « skill » ni « historique » (« qu'est-ce que je demande tout le temps à Copilot ? », « which prompts do I keep repeating? »).
---

# Analyse de l'historique GitHub Copilot

Ton rôle : répondre, preuves à l'appui, à la question **« qu'est-ce qui mérite d'être automatisé dans ma façon de travailler avec Copilot, et qu'est-ce qui ne le mérite pas ? »**

Le partage du travail est simple : le script compte (tours, corrections, demandes récurrentes, commandes répétées), toi tu juges (cause racine, décision, rapport). Tout reste en local, aucune donnée n'est envoyée. L'historique des conversations n'est d'ailleurs pas visible des administrateurs de l'organisation : seul l'utilisateur, sur sa machine, peut faire cette analyse.

## Où se trouve l'historique

| Outil | Emplacement |
|---|---|
| Copilot CLI | `~/.copilot/session-state/<id>/events.jsonl` (+ `workspace.yaml`), index `~/.copilot/session-store.db` |
| VS Code (Copilot Chat) | `<User>/workspaceStorage/<hash>/chatSessions/*.jsonl` (ou `.json` avant VS Code 1.109) et `<User>/globalStorage/emptyWindowChatSessions/` |
| Agent Copilot dans IntelliJ ou VS Code | Les versions récentes font tourner leurs conversations sur l'agent Copilot CLI : elles sont dans `~/.copilot/session-state/<id>/`, avec `client_name: copilot-intellij` ou `vscode-agent-host` dans `workspace.yaml`. Le script les classe par IDE (source `jetbrains` ou `vscode`). |
| JetBrains, base native | `%LOCALAPPDATA%\github-copilot\<ide>\chat-agent-sessions\<id>\copilot-*-nitrite.db` sous Windows, `~/.config/github-copilot/...` sous macOS et Linux. Avec un plugin récent, elle ne contient que des métadonnées : le texte est dans `session-state`, déjà lu. Avec un plugin ancien, elle contient les conversations. **Verrouillée tant qu'IntelliJ est ouvert** : le digest le signale, il faut fermer l'IDE pour la lire. |

`<User>` vaut `%APPDATA%\Code\User` sous Windows, `~/Library/Application Support/Code/User` sous macOS et `~/.config/Code/User` sous Linux. Les dossiers Insiders et VSCodium sont détectés aussi.

**JetBrains, lecture best-effort.** Les bases Nitrite sont un format binaire Java non documenté. Le script en extrait les demandes du mode Agent, mais pas celles du mode Ask, et ne connaît que la date du fichier. Dans le rapport, signale ces limites pour les sessions JetBrains. Si `--list-sources` trouve des bases mais que le digest ne contient aucune session JetBrains, la version du plugin n'est probablement pas encore reconnue. Fais alors lancer `--jetbrains-diagnose --no-excerpts` et transmettre le résultat au mainteneur des skills : ce diagnostic ne décrit que la structure, sans aucun texte de conversation.

**Non couverts**, car non stockés localement : Copilot Chat sur github.com, Visual Studio, Xcode, et l'agent cloud de Copilot. Si l'utilisateur travaille surtout avec ces outils, dis-le dès le départ.

## Complément natif : `/chronicle` (Copilot CLI)

Copilot CLI propose `/chronicle tips`, `/chronicle improve` (propose des ajouts à `.github/copilot-instructions.md` pour le dépôt courant), `/chronicle search <mot>` et `/chronicle standup`. Ces commandes sont interactives et limitées à la CLI ; il n'existe pas de commande `copilot chronicle … --format json`.

Ce skill va plus loin : il couvre plusieurs projets, inclut VS Code, chiffre les répétitions et tranche entre skill, instruction et rien. Quand une recommandation se résume à une règle pour le dépôt courant et que l'utilisateur est dans la CLI, mentionne `/chronicle improve` comme moyen rapide de l'appliquer.

## Déroulé

### 1. Cadrer, sans sur-questionner
Par défaut : les 30 derniers jours, toutes les sources présentes (CLI, VS Code, JetBrains) et tous les projets. Ne pose une question que si la demande est vraiment ambiguë.

Distingue deux types d'audit, et annonce le type choisi en tête du rapport :
- **Audit global** : c'est le cas par défaut, et en particulier quand l'utilisateur dit « depuis le début », « en général » ou « sur tout mon historique » (alors `--days 0`). Le rapport couvre tous les projets du digest avant toute recommandation propre à un dépôt.
- **Audit d'un dépôt** : seulement si l'utilisateur parle de « ce dépôt » ou en nomme un. Filtre alors avec `--project <nom du dossier>`.

Le dossier où tu travailles ne décide pas du périmètre. Un audit global lancé depuis un dépôt reste global.

### 2. Produire le digest
Le script se trouve dans `scripts/history_audit.py`, à côté de ce fichier : `~/.copilot/skills/copilot-session-analyzer/` en installation personnelle, `.github/skills/copilot-session-analyzer/` dans un dépôt. Il n'utilise que la bibliothèque standard de Python 3.8+.

```bash
python "<dossier du skill>/scripts/history_audit.py" --source copilot --days 30 --out "<dossier temporaire>/copilot-digest.md"
```

- Lance-le avec l'outil terminal (mode agent ou CLI). En mode Ask, sans terminal, donne la commande à l'utilisateur et demande-lui de joindre le fichier produit.
- Sous Windows, utilise `python` ou `py` ; sous macOS et Linux, `python3`.
- Écris le digest hors du dépôt (dossier temporaire), pour ne pas le committer par erreur.
- Si le digest est vide, lance `--list-sources`, puis élargis avec `--days 0`. Si VS Code est installé à un endroit inhabituel, passe `--vscode-user-dir <chemin du dossier User>`.
- Si tu connais le dossier du dépôt (l'utilisateur te l'a donné, ou tu y travailles), passe `--cwd <dossier du dépôt>` : c'est là que le script cherche les instructions, prompt files et skills déjà en place. `--cwd` sert **uniquement** à vérifier l'existant : il ne restreint pas les sessions analysées et ne doit pas orienter les recommandations vers ce dépôt. Si les limites du digest signalent des dossiers de projet introuvables, l'existant n'a pas été vérifié : ne conclus pas que rien n'existe.
- La conversation d'audit en cours fait partie de l'historique : ignore-la dans ta lecture.

### 3. Lire, puis vérifier sans tout relire
Le digest contient déjà de quoi juger :
- les épisodes de friction avec leur contexte (demande, fin de réponse, réaction) ;
- les garde-fous tapés par l'utilisateur (« sans coder », « ne fais pas le build », fautes de frappe comprises) ;
- les défauts qu'il a signalés après une livraison (« ça dépasse », « ne s'affiche pas ») ;
- les questions de confirmation posées par l'assistant (« je lance les tests ? » suivi de « oui ») ;
- les « non » qui répondent à une proposition de l'assistant, comptés à part et non comme des corrections ;
- les outils les plus utilisés et leurs erreurs les plus fréquentes ;
- le coût côté assistant : les erreurs d'outils regroupées par cause (mauvais dossier, interpréteur introuvable, syntaxe du shell, commande qui échoue sans message…) ;
- les sessions actives en même temps sur le même projet, et les modifications qu'une session attribue à une autre ;
- avec `--project`, les sessions sans projet identifié (fenêtre VS Code vide, ancienne base JetBrains), listées à part : regarde si elles concernent le dépôt ;
- les commandes répétées.

Lis-le, puis `references/methodology.md` (sections 2 à 4). Pour chaque épisode, nomme la cause racine (A à I) : c'est elle qui détermine le remède, pas le nombre de tours.

Ne te limite pas aux épisodes de friction : ils ne montrent que ce que l'utilisateur a dit. Les sections « Coût côté assistant » et « Sessions en parallèle » révèlent ce qu'il ne voit pas (causes H et I). Quand tu cites une demande récurrente, montre ses variantes de formulation réelles plutôt que de dire qu'elle est « toujours la même ».

Ensuite, vérifie au plus juste :
- **Une hypothèse** (« je demande souvent X ? ») : `--grep "motif"`, ou `--grep "motif" --grep-replies` pour chercher dans les réponses de Copilot. Une commande donne les occurrences, les sessions, les jours et des exemples.
  - Ajoute toujours `--grep-log "<dossier temporaire>/greps.md"` : chaque recherche est conservée, avec son motif et ses exemples.
  - Lis les exemples avant de citer le chiffre. Un exemple hors sujet (« prendre en compte » pour un compte de test, « le like sur tiktok » pour une publication) veut dire que le motif est trop large : resserre-le (mots plus précis, noms propres, verbes d'action) et relance. Ne cite que le chiffre du motif final, qui ne ramène plus de faux positif.
- **Un épisode ambigu** : `--show-session <id>` affiche une vue compacte (tours signalés et leur contexte). Ouvre **5 sessions au maximum**, et seulement si le digest ne permet pas de nommer la cause. Réserve `--full` aux cas exceptionnels.
- **N'écris pas tes propres scripts d'extraction et ne lis pas les fichiers de session directement.** Si une information manque au digest, signale-le dans les limites du rapport.

Pourquoi ce budget : le digest coûte quelques milliers de tokens. Relire chaque session en coûte des dizaines de milliers, pour très peu d'information en plus. L'audit doit rester moins cher que le temps qu'il fait gagner.

Croise enfin avec la section « Déjà en place » : skills, prompt files (`.github/prompts/`), `copilot-instructions.md` et fichiers `*.instructions.md` existants. Si un outil couvre déjà le besoin, recommande de l'améliorer ou de le faire connaître, jamais de le dupliquer.

### 4. Décider
Applique la matrice de `references/methodology.md`, puis traduis chaque mécanisme générique en mécanisme Copilot :

| Mécanisme générique | Dans GitHub Copilot | Emplacement |
|---|---|---|
| Instruction persistante | Instructions du dépôt | `.github/copilot-instructions.md` (lu par Copilot Chat, la CLI et l'agent) ; `AGENTS.md` est aussi lu par les agents |
| Instruction ciblée | Fichier d'instructions par chemin | `.github/instructions/<nom>.instructions.md` avec `applyTo: "**/*.java"` dans l'en-tête |
| Skill | Agent skill | `.github/skills/<nom>/SKILL.md` (équipe, versionné) ou `~/.copilot/skills/<nom>/SKILL.md` (perso). Le `name` doit être identique au nom du dossier. |
| Prompt réutilisable | Prompt file | `.github/prompts/<nom>.prompt.md`, invoqué par `/<nom>` dans VS Code |
| Automatisation déclenchée | Hook, ou contrôle automatisé | `.github/hooks/*.json` (`preToolUse`…, pour la CLI et l'agent) ; un contrôle automatisé sur chaque PR pour ce qui doit être garanti |
| Agent personnalisé | Custom agent | `.github/agents/<nom>.agent.md` |
| Accès externe | Serveur MCP | `.vscode/mcp.json` (VS Code) ou configuration MCP de la CLI (`/mcp`) |

Pour distinguer instruction et skill : si la correction tient en une phrase (« assertions AssertJ », « erreurs au format RFC 7807 »), c'est une instruction, éventuellement ciblée par `applyTo`. S'il faut une procédure, des exemples ou un script, c'est un skill.

Pour choisir entre une portée personnelle et une portée équipe : si la répétition vient de conventions du dépôt, la solution va dans `.github/`, afin que toute l'équipe en profite. Si elle vient des habitudes de l'utilisateur, elle va dans `~/.copilot/`.

### 5. Rendre le rapport
Suis `references/report-template.md`. Pour chaque skill recommandé, fournis un brouillon de `SKILL.md` dont la `description` reprend les formulations réelles relevées dans l'historique. Chaque recommandation donne un emplacement de fichier et un gain chiffré.

Avant de rendre le rapport, relis-le avec la section 6 de la méthodologie :
- un même chiffre est identique dans toutes les sections, avec un périmètre annoncé. Préfère les chiffres du digest ; pour un chiffre tiré de `--grep`, cite le motif ;
- le « En bref » ne dit pas plus que le corps : des ratios, pas « toujours » ni « à chaque fois » ; une hypothèse reste présentée comme une hypothèse ; le nombre de recommandations et de thèmes écartés correspond aux sections ;
- un thème n'est classé « sous le seuil » qu'après un `--grep` de vérification ;
- les limites décrivent la méthode réellement suivie (sessions ouvertes, recherches).

Respecte la confidentialité : courts extraits uniquement, aucun secret, aucun nom de client, et les conversations hors travail résumées en une catégorie.

Termine en proposant de créer la recommandation n° 1. Si le plugin `skill-creator` est installé, propose-le pour créer et tester le skill. N'écris rien dans `.github/` ni dans `~/.copilot/` sans l'accord explicite de l'utilisateur : ces fichiers changent le comportement de Copilot pour lui, ou pour toute l'équipe.

## Options utiles du script

| Option | Effet |
|---|---|
| `--source copilot-cli` / `vscode` / `jetbrains` | Une seule source |
| `--jetbrains-diagnose` | Décrit les bases JetBrains trouvées (structure + 3 courts extraits ; `--no-excerpts` pour aucun texte) |
| `--days N` | Sessions actives dans les N derniers jours (`0` = tout) |
| `--project texte` | Filtre sur le nom ou le chemin du projet |
| `--exclude-project texte` | Ignore les sessions dont le nom de projet ou le chemin contient ce texte (ex. `billing-api`) |
| `--cwd dossier` | Dossier du dépôt où chercher l'existant (instructions, prompt files, skills) ; ne change pas le périmètre |
| `--grep "motif"` | Compte et illustre les demandes qui correspondent (`--grep-replies` : dans les réponses) |
| `--show-session id` | Vue compacte d'une session (`--full` : tous les tours) |
| `--format json` | Sortie structurée |
| `--similarity 0.35` | Groupes de demandes plus larges (0.4 par défaut ; monter à 0.5 pour des groupes plus stricts) |
| `--copilot-home`, `--vscode-user-dir`, `--jetbrains-dir` | Emplacements non standard |
| `--source all` | Ajoute l'historique Claude Code s'il existe sur la machine |
