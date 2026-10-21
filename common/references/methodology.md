# Méthodologie : de l'historique à la décision

Ce document est commun aux skills `claude-session-analyzer` et `copilot-session-analyzer`.
Les mécanismes y sont nommés de façon générique (« instruction persistante », « skill »…) ;
le `SKILL.md` de chaque plateforme donne leur traduction concrète (fichier, emplacement).

## Sommaire
1. Ce que mesure le digest
2. Trouver la cause racine d'une session qui tourne en rond
3. Valider les demandes récurrentes
4. Décider : la matrice
5. Estimer le gain
6. Pièges fréquents

---

## 1. Ce que mesure le digest

| Signal | Définition | Ce qu'il suggère |
|---|---|---|
| **Tour** | Une demande de l'utilisateur et tout ce que l'assistant fait jusqu'à la suivante | Beaucoup de tours ≠ problème : une grosse fonctionnalité en demande beaucoup. C'est le *ratio* corrections / tours qui compte. |
| **Correction** | Demande (hors première) qui commence par « non », « toujours pas », « je t'ai dit », « tu as oublié »… | L'assistant a fait autre chose que ce qui était attendu. |
| **Reformulation** | Deux demandes consécutives presque identiques | La première formulation n'a pas suffi. |
| **Interruption** | L'utilisateur a stoppé l'assistant (Échap, bouton stop, requête annulée) | L'assistant partait dans la mauvaise direction. |
| **Refus d'outil** | L'utilisateur a refusé une action (commande, édition) | Action jugée risquée ou hors sujet. |
| **Erreur d'outil** | Une commande ou un outil a échoué | Environnement mal connu de l'assistant, ou tâtonnement. |
| **Vote négatif / échec** | 👎 ou réponse en erreur (VS Code) | Insatisfaction explicite. |
| **Garde-fou** | L'utilisateur prévient au lieu de corriger : « sans coder », « ne fais pas le build », « attends mon go » | Une règle que l'assistant devrait déjà connaître : instruction persistante. |
| **Défaut signalé** | Après une livraison, l'utilisateur trouve lui-même le problème : « ça dépasse », « ne s'affiche pas » | L'assistant ne vérifie pas son travail : une étape de vérification manque (skill ou instruction). |
| **Question de confirmation** | L'assistant finit par « je commite ? », « je le fais ? » et l'utilisateur répond « oui » | Une permission toujours accordée : une règle d'autonomie supprime ce tour. |
| **Code réécrit** | Un outil reçoit des centaines de scripts différents (requêtes SQL, JavaScript de navigateur…) | Une aide réutilisable (fonction, script) manque. |
| **Mémoire volumineuse** | Une note de mémoire longue, qui grossit de session en session | Souvent une procédure qui devrait être un skill. |
| **Erreurs d'outils par cause** | Échecs regroupés par cause : mauvais dossier, interpréteur introuvable, syntaxe du shell, délai dépassé, édition ratée, script qui échoue sans message | Un coût que l'utilisateur ne voit pas : il ne s'en plaindra jamais, mais il le paie à chaque session. |
| **Sessions en parallèle** | Sessions actives en même temps sur le même projet, et modifications qu'une session attribue à une autre (« ne vient pas de moi ») | Des sessions qui se marchent dessus : commits mélangés, fichiers inexpliqués, serveurs arrêtés. |

Le **score de friction** sert uniquement à classer les sessions à lire en priorité. Ce n'est pas une mesure de qualité : une session au score élevé peut être parfaitement normale (débogage d'un bug inconnu, par exemple).

## 2. Trouver la cause racine d'une session qui tourne en rond

Pars des épisodes du digest : demande, fin de réponse, réaction. N'ouvre la session (`--show-session <id>`, vue compacte) que si l'épisode reste ambigu. Pour chaque épisode de friction, rattache-le à **une** cause :

| Cause | Symptômes typiques | Remède générique |
|---|---|---|
| **A. Contexte manquant** | La même correction revient d'une session à l'autre (« on utilise AssertJ », « les fichiers générés vont dans build/ ») ; l'assistant ignore une convention, un outil interne, l'architecture | Instruction persistante si la règle tient en 1 à 3 lignes ; skill de connaissance si le savoir est riche (exemples, cas particuliers) |
| **B. Procédure refaite à la main** | Même enchaînement de demandes ou de commandes à chaque fois (build → tests → commit → push → tag) | Skill procédural, avec un script si les étapes sont déterministes |
| **C. Demande sous-spécifiée** | L'assistant devine mal puis l'utilisateur précise ; reformulations | Prompt réutilisable ou skill qui pose les bonnes questions au départ |
| **D. Environnement qui casse** | Mêmes erreurs d'outil sur la même commande ; refus de permissions répétés | Réparer l'environnement ; documenter la bonne commande (instruction) ; script fiable. Un skill ne corrige pas une machine cassée. |
| **E. Préférence de style** | « réponds en français », « plus court », « pas d'emoji », « pas de commentaires » | Instruction persistante, jamais un skill |
| **F. Travail exploratoire ou créatif** | Itérations de design, débogage d'un problème nouveau, brainstorming | Rien : ces tours sont la valeur du travail |
| **G. Limite de l'assistant** | Échoue durablement sur une tâche difficile, quelle que soit la formulation | Rien à automatiser ; éventuellement changer d'approche ou d'outil |
| **H. Sessions qui se marchent dessus** | Plusieurs sessions actives en même temps dans les mêmes dossiers ; « une modification qui ne vient pas de moi » ; « c'est toi ou une autre session ? » ; « tout est commité ? » ; un serveur ou un outil arrêté par une autre session | Instruction : une seule session modifie le code à la fois ; un worktree git (ou une copie séparée) par chantier parallèle ; un récapitulatif de l'état des dépôts en fin de session |
| **I. Coût côté assistant** | Erreurs d'outils récurrentes que l'utilisateur ne voit pas : mauvais dossier courant, interpréteur introuvable, guillemets cassés dans le shell, recherches qui expirent sur des dossiers énormes, script du projet qui échoue sans dire pourquoi, vérification par le chemin le plus long (build complet au lieu d'un test ciblé) | Instruction qui décrit l'environnement (shell, interpréteurs, dépôts, la bonne façon de vérifier) ; réparer le script du projet pour qu'il affiche l'échec ; exclure ou ranger les dossiers lourds ; rien si c'est isolé |

La question à se poser : *« Si l'assistant avait su X dès le départ, ces tours auraient-ils disparu ? »* Si oui, X est ce qu'il faut capturer (règle, skill, script). Si non (F, G), il n'y a rien à automatiser.

**Ne t'arrête pas à ce que l'utilisateur dit.** Les causes H et I ne laissent souvent aucune correction : l'utilisateur ne voit pas les erreurs d'outils, et une collision entre sessions se présente comme un « fichier modifié » que l'assistant contourne. Lis toujours les sections « Coût côté assistant » et « Sessions en parallèle » du digest, même quand les épisodes de friction semblent tout expliquer.

**Cherche aussi le savoir découvert en cours de route** (cause A, mais tardive) : une règle métier ou une contrainte (juridique, réglementaire, client, clinique…) apprise au milieu d'un chantier et qui a fait refaire du travail. Indices : « on n'a pas le droit de… », « le client / l'avocat a dit… », un livrable repris de zéro, une session dédiée à corriger après coup. Remède : un document de référence versionné dans le projet, et une ligne d'instruction qui demande de le lire avant ce type de travail. `--grep` sur les mots du domaine aide à le retrouver.

## 3. Valider les demandes récurrentes

Les groupes du digest sont construits par similarité de mots : ils proposent des pistes, ils ne prouvent rien.

1. **Même intention ?** Lis les exemples. Même verbe et même type d'objet (« génère les tests de *X* », « génère les tests de *Y* ») : c'est une tâche récurrente. Mêmes mots mais besoins différents : c'est un faux positif.
2. **Fusionner et compléter.** Une même tâche peut être éclatée en plusieurs groupes (« écris » contre « génère »), ou absente parce qu'elle a été formulée en une phrase très courte. Relis les premières demandes des sessions à forte friction.
3. **Croiser avec les commandes shell et les outils.** Une suite de commandes présente dans beaucoup de sessions (`git add` + `git commit` + `git push`, build, tests) révèle une procédure même quand les demandes sont courtes (« commit », « parfait, push »). Il en va de même pour un outil qui reçoit sans cesse du code écrit à la main.
   - Pour vérifier une intuition, utilise `--grep "motif"` plutôt que de relire des sessions.
4. **Écarter les copies.** Des demandes identiques le même jour dans deux sessions viennent souvent d'une reprise de session (`--resume`) ou d'une même conversation relancée. Le script dédoublonne les reprises exactes, pas les copies manuelles.
5. **Vérifier un thème avant de l'écarter.** Les groupes du digest sous-comptent les demandes formulées autrement (« supprime le compte X », « remets la base à zéro », « prépare le JDD »). Avant de classer un thème « sous le seuil » ou « rien » sur la foi d'un groupe, compte-le avec un `--grep` sur ses mots-clés et cite ce compte.
6. **Vérifier l'existant.** Consulte la section « Déjà en place ». Si une commande ou un skill couvre déjà la tâche, la recommandation devient « l'améliorer » ou « le faire connaître » (s'il existe mais n'est pas utilisé). Ne recrée jamais un doublon.

## 4. Décider : la matrice

Quatre critères, à évaluer sur la période analysée :

- **Fréquence** : au moins 3 sessions sur au moins 2 jours différents, ou au moins 2 fois par semaine.
- **Coût** : tours moyens des sessions concernées, nombre de corrections, nombre d'étapes manuelles.
- **Stabilité** : la façon de faire est la même à chaque fois. Si elle change d'une fois sur l'autre, l'automatiser figerait une cible mouvante.
- **Spécificité** : le savoir nécessaire est propre à l'utilisateur ou au projet (conventions, scripts, outils internes). Si c'est du savoir général que l'assistant possède déjà, il n'y a rien à capturer.

| Situation constatée | Mécanisme recommandé |
|---|---|
| La même correction courte revient (règle de 1 à 3 lignes) | **Instruction persistante** (profil ou projet) |
| L'assistant demande souvent une permission que l'utilisateur accorde toujours (« je commite ? » → « oui ») | **Instruction persistante** : « fais X sans demander » |
| L'utilisateur tape souvent le même garde-fou (« sans coder », « ne build pas ») | **Instruction persistante** : « une question appelle une réponse, pas une modification », « pas de build sans demande » |
| L'utilisateur trouve lui-même les défauts après livraison | **Étape de vérification** à ajouter (dans un skill, ou en instruction si elle tient en une ligne) |
| Le même genre de code est réécrit à chaque session dans un outil | **Script ou fonction d'aide**, appelé par un skill si une procédure l'entoure |
| La règle ne concerne qu'un type de fichier ou un dossier | **Instruction ciblée** |
| Procédure multi-étapes, fréquente, stable, avec du savoir spécifique | **Skill** (avec script pour les étapes déterministes) |
| Même demande de départ, sans procédure ni ressources | **Prompt réutilisable / commande** |
| Quelque chose doit se produire *à chaque fois*, sans dépendre de la mémoire de l'assistant (formater, bloquer une commande, lancer un contrôle) | **Automatisation déclenchée** (hook, contrôle automatisé) |
| Besoin d'un rôle isolé, avec ses propres outils et son propre contexte | **Agent personnalisé** |
| L'assistant n'a pas accès à un système externe dont il a besoin | **Serveur MCP** |
| Une commande ou un skill existe déjà mais est insuffisant ou oublié | **Améliorer ou faire connaître l'existant** |
| Des sessions parallèles se marchent dessus (cause H) | **Instruction persistante** (une session de code à la fois, worktree par chantier), éventuellement une **automatisation déclenchée** qui signale les modifications non commitées au démarrage |
| Les mêmes erreurs d'outils reviennent (cause I) | **Instruction** qui décrit l'environnement, ou **réparer le script / le dossier** en cause. Ce n'est pas un skill. |
| Ponctuel, exploratoire (cause F), limite de l'assistant (G), ou déjà bien couvert nativement | **Ne rien faire**, et le dire explicitement |

**Un verdict par thème, tiré des critères, pas de l'impression.** Chaque thème qui atteint le seuil de fréquence figure dans le tableau des tâches répétitives avec son verdict et le critère qui le fonde. Deux runs sur les mêmes données doivent aboutir au même verdict :
- un projet à l'arrêt ne fait pas passer un thème à « rien » : garde le verdict que donnent les critères et ajoute « à faire si le projet reprend » ;
- un thème sous le seuil reste « rien », ou « à surveiller » s'il coûte cher, même si une règle serait facile à écrire ;
- un skill recommandé en plus d'une règle doit supprimer des tours que la règle ne supprime pas déjà. Chiffre-les, sinon recommande la règle seule.

Plusieurs mécanismes se combinent souvent : par exemple une règle courte dans les instructions et un skill pour la procédure complète. Recommande le plus léger qui résout le problème. Un skill est plus coûteux à maintenir qu'une ligne d'instruction.

## 5. Estimer le gain

Garde un ordre de grandeur et montre le calcul :

> tours économisés par mois ≈ occurrences par mois × (tours moyens actuels − 1 à 2 tours attendus avec l'automatisation)

N'invente pas de minutes ni d'euros : l'historique ne mesure pas le temps passé à réfléchir entre deux messages.

Chaque recommandation a un ordre de grandeur, même quand le gain n'est pas en tours de l'utilisateur. Dans ce cas, chiffre ce que l'historique mesure : erreurs d'outils évitées par mois, appels d'outils en moins, sessions concernées. « Difficile à chiffrer » ne suffit pas : donne au moins le nombre d'épisodes de la période et leur extrapolation au mois.

## 6. Pièges fréquents

- **Trop de recommandations.** Deux à quatre recommandations solides valent mieux que dix faibles. Classe-les par gain estimé.
- **Chiffres incohérents.** Un même chiffre doit être identique partout dans le rapport. Pour chaque chiffre clé, garde une seule source et un seul périmètre :
  - préfère les chiffres du digest : ils sont reproductibles d'une analyse à l'autre ;
  - un chiffre tiré d'un `--grep` dépend du motif choisi. Écris le motif à côté (« 96 demandes, motif `commit|push|merge` »), garde le même motif dans tout le rapport, et vérifie que l'étiquette décrit exactement ce que le motif compte. Un motif qui attrape aussi d'autres phrases (« ne vient pas de moi ») ne peut pas s'appeler « commits non poussés » ;
  - si deux comptages diffèrent (motif plus large, sessions d'audit incluses ou non), dis lequel tu retiens et pourquoi.
- **Résumé plus fort que le corps.** Le « En bref » reprend les chiffres du corps, sans les arrondir vers le haut. « Toujours », « à chaque fois », « presque toujours » ne s'emploient que si c'est littéralement le cas (17 « oui » sur 39 questions, ce n'est pas « presque toujours ») ; sinon, écris le ratio. « Rien n'est en place » ne s'écrit que si l'existant a pu être vérifié dans le dépôt. Une hypothèse (« la moitié des scripts sert aux captures ») reste une hypothèse dans le résumé. Les décomptes du résumé (« 2 skills, 2 règles, 7 thèmes écartés ») correspondent exactement aux sections du rapport.
- **Motif `--grep` trop large ou trop étroit.** Un motif choisi au jugé compte autre chose que ce que dit son étiquette : « 36 demandes de publication » qui incluent « c'est moi le like sur tiktok », ou un compte de test noyé sous « prendre en compte ». Lis toujours les exemples, resserre le motif jusqu'à ce qu'ils soient tous dans le sujet, et cite le chiffre du motif final. Garde les recherches avec `--grep-log`.
- **Verdict qui dépend de l'humeur.** Applique les critères de la section 4. Un thème vu dans au moins 3 sessions sur au moins 2 jours, avec un coût mesurable, ne peut pas être classé « rien » sans raison explicite (« n'est plus revenu depuis le 04/10 », « déjà couvert par /x »).
- **« Déjà en place » vide ne prouve pas que rien n'existe.** Le digest ne cherche l'existant que dans le profil, dans `--cwd` et dans les dossiers de projet présents sur la machine. Si les limites signalent des dossiers introuvables, relance avec `--cwd <dossier du dépôt>`, ou écris que l'existant n'a pas pu être vérifié.
- **Sessions sans projet identifié.** Avec `--project`, les sessions dont le stockage ne dit pas le dossier (fenêtre VS Code vide, ancienne base JetBrains) sont listées à part. Regarde si elles concernent le projet avant de les ignorer.
- **« non » n'est pas toujours une correction** : ce peut être la réponse à une question de l'assistant (« Je l'ajoute ? » → « Non pas de suite »). Le digest les compte à part (« non » en réponse à une proposition) et ne les inclut pas dans les corrections. Ne les y remets pas : cite-les comme signe que l'assistant propose du travail non demandé, pas comme une erreur de sa part.
- **Session longue ≠ session ratée.** Regarde le ratio corrections / tours et la cause.
- **La session d'audit elle-même** apparaît dans l'historique (elle parle d'historique et de skills) : ignore-la.
- **Confidentialité.** L'historique peut contenir des sujets personnels (santé, juridique, finances), des noms de clients, des secrets. Ne cite que de courts extraits utiles à la démonstration. Pour les sessions sans rapport avec le travail, donne au plus la catégorie (« 3 sessions personnelles non détaillées »). Ne recopie jamais de secret, de mot de passe ou de donnée nominative. Le rapport reste local.
- **Données partielles.** Seul l'historique stocké sur cette machine est lu, et certains outils purgent l'historique ancien. Signale la période réellement couverte.
