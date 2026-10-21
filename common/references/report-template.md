# Modèle de rapport d'audit

Utilise cette structure. Adapte la longueur à la quantité de données : un historique de 5 sessions mérite une page, pas dix. Écris dans la langue de l'utilisateur.

```markdown
# Audit de l'historique <Claude Code | GitHub Copilot> — <date de début> → <date de fin>

## En bref
<Mêmes chiffres que le corps, avec des ratios plutôt que « toujours » ou « presque toujours ».>
- <Verdict principal, ex. : « 2 automatisations valent le coup, 1 règle à ajouter, le reste ne mérite rien »>
- <Le plus gros gisement de tours perdus, en une phrase, ex. : « 17 des 39 questions « je commite ? » reçoivent un simple oui »>
- <Ce qui fonctionne déjà bien (commandes ou skills existants utilisés). Si l'existant n'a pas pu être vérifié (dépôt introuvable), écris-le au lieu de « rien n'est en place ».>

## Chiffres clés
| Sessions | Demandes | Tours médians / session | Sessions ≥ 8 tours | Sessions avec corrections |
|---|---|---|---|---|
| … | … | … | … | … |

## Là où ça tourne en rond
<3 à 5 épisodes maximum, les plus coûteux. Pour chacun :>
- **<Session, date, projet>** : <ce qui s'est passé, en une phrase>. Cause : <A–I, voir méthodologie>. Indice : « <extrait court> » ou <signal chiffré>.

## Ce que l'assistant pourrait éviter de lui-même
<Ce que l'utilisateur ne voit pas ou ne dit pas : erreurs d'outils récurrentes (cause I), sessions parallèles qui se marchent dessus (cause H). Une ligne par problème qui revient ; « rien de notable » si les sections du digest sont vides ou anecdotiques.>
| Problème | Combien | Exemple | Remède |
|---|---|---|---|
| … | … erreurs dans … sessions | `<commande>` → « <erreur> » | <instruction, script à réparer, rien> |

## Tâches répétitives
| Thème | Fréquence (sessions / jours) | Tours moyens | Ce qui se répète | Verdict |
|---|---|---|---|---|
| … | … | … | … | Skill / Règle / Prompt / Rien |

## Recommandations, par priorité

### 1. <Créer un skill | Ajouter une règle | Prompt réutilisable | Hook / contrôle automatisé | Améliorer l'existant> : <nom>
- **Preuves** : <occurrences, sessions, corrections typiques>
- **Gain estimé** : <calcul en tours économisés par mois ; à défaut, erreurs ou appels d'outils évités par mois, jamais seulement « difficile à chiffrer »>
- **Emplacement** : `<chemin exact>`
- **Brouillon** : <prêt à copier, voir ci-dessous>

### 2. …

## Ce qui ne mérite pas d'automatisation
- <Thème> : <pourquoi (exploratoire, ponctuel, déjà couvert nativement…)>

## Limites de l'analyse
- <Période réellement couverte, sources lues et non lues, heuristiques>
- <Méthode, telle qu'elle a vraiment été suivie : sessions ouvertes (ids), recherches lancées (motifs)>
```

## Brouillon de skill (quand la recommandation est « créer un skill »)

```markdown
---
name: <kebab-case, identique au nom du dossier>
description: <Ce que fait le skill>. À utiliser quand <contexte>, par exemple quand l'utilisateur demande « <formulation réelle relevée dans l'historique> » ou « <autre formulation réelle> ».
---

# <Titre>

## Contexte à connaître
<Le savoir que l'assistant ne pouvait pas deviner : conventions, chemins, outils internes, pièges rencontrés dans l'historique.>

## Étapes
1. …
2. …

## Vérifications avant de rendre la main
- <Ce que l'utilisateur corrigeait à chaque fois, transformé en contrôle>
```

Dans la `description`, reprends les **formulations réelles** de l'utilisateur relevées dans l'historique : c'est ainsi qu'il formulera ses prochaines demandes, et c'est ce texte qui déclenche le skill.

Transforme chaque **correction récurrente** en instruction explicite ou en contrôle final : ce sont exactement les tours que le skill doit faire disparaître.

## Brouillon de règle (quand la recommandation est « instruction persistante »)

Une règle tient en une ou deux lignes, à l'impératif, avec la raison si elle n'est pas évidente :

```markdown
- Tests Java : assertions AssertJ (pas `assertEquals`), un `@DisplayName` en français par test.
```
