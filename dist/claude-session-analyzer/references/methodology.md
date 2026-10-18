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

La question à se poser : *« Si l'assistant avait su X dès le départ, ces tours auraient-ils disparu ? »* Si oui, X est ce qu'il faut capturer (règle, skill, script). Si non (F, G), il n'y a rien à automatiser.

## 3. Valider les demandes récurrentes

Les groupes du digest sont construits par similarité de mots : ils proposent des pistes, ils ne prouvent rien.

1. **Même intention ?** Lis les exemples. Même verbe et même type d'objet (« génère les tests de *X* », « génère les tests de *Y* ») : c'est une tâche récurrente. Mêmes mots mais besoins différents : c'est un faux positif.
2. **Fusionner et compléter.** Une même tâche peut être éclatée en plusieurs groupes (« écris » contre « génère »), ou absente parce qu'elle a été formulée en une phrase très courte. Relis les premières demandes des sessions à forte friction.
3. **Croiser avec les commandes shell et les outils.** Une suite de commandes présente dans beaucoup de sessions (`git add` + `git commit` + `git push`, build, tests) révèle une procédure même quand les demandes sont courtes (« commit », « parfait, push »). Il en va de même pour un outil qui reçoit sans cesse du code écrit à la main.
   - Pour vérifier une intuition, utilise `--grep "motif"` plutôt que de relire des sessions.
4. **Écarter les copies.** Des demandes identiques le même jour dans deux sessions viennent souvent d'une reprise de session (`--resume`) ou d'une même conversation relancée. Le script dédoublonne les reprises exactes, pas les copies manuelles.
5. **Vérifier l'existant.** Consulte la section « Déjà en place ». Si une commande ou un skill couvre déjà la tâche, la recommandation devient « l'améliorer » ou « le faire connaître » (s'il existe mais n'est pas utilisé). Ne recrée jamais un doublon.

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
| Quelque chose doit se produire *à chaque fois*, sans dépendre de la mémoire de l'assistant (formater, bloquer une commande, lancer un contrôle) | **Automatisation déclenchée** (hook, CI) |
| Besoin d'un rôle isolé, avec ses propres outils et son propre contexte | **Agent personnalisé** |
| L'assistant n'a pas accès à un système externe dont il a besoin | **Serveur MCP** |
| Une commande ou un skill existe déjà mais est insuffisant ou oublié | **Améliorer ou faire connaître l'existant** |
| Ponctuel, exploratoire (cause F), limite de l'assistant (G), ou déjà bien couvert nativement | **Ne rien faire**, et le dire explicitement |

Plusieurs mécanismes se combinent souvent : par exemple une règle courte dans les instructions et un skill pour la procédure complète. Recommande le plus léger qui résout le problème. Un skill est plus coûteux à maintenir qu'une ligne d'instruction.

## 5. Estimer le gain

Garde un ordre de grandeur et montre le calcul :

> tours économisés par mois ≈ occurrences par mois × (tours moyens actuels − 1 à 2 tours attendus avec l'automatisation)

N'invente pas de minutes ni d'euros : l'historique ne mesure pas le temps passé à réfléchir entre deux messages.

## 6. Pièges fréquents

- **Trop de recommandations.** Deux à quatre recommandations solides valent mieux que dix faibles. Classe-les par gain estimé.
- **« non » n'est pas toujours une correction** : ce peut être la réponse à une question de l'assistant (« non, pas besoin de tests »). Vérifie le contexte avant de compter un épisode.
- **Session longue ≠ session ratée.** Regarde le ratio corrections / tours et la cause.
- **La session d'audit elle-même** apparaît dans l'historique (elle parle d'historique et de skills) : ignore-la.
- **Confidentialité.** L'historique peut contenir des sujets personnels (santé, juridique, finances), des noms de clients, des secrets. Ne cite que de courts extraits utiles à la démonstration. Pour les sessions sans rapport avec le travail, donne au plus la catégorie (« 3 sessions personnelles non détaillées »). Ne recopie jamais de secret, de mot de passe ou de donnée nominative. Le rapport reste local.
- **Données partielles.** Seul l'historique stocké sur cette machine est lu, et certains outils purgent l'historique ancien. Signale la période réellement couverte.
