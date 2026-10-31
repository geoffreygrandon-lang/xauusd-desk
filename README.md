# Macro Desk — XAU/USD

Tableau de bord pour lire l'or spot (XAU/USD) : séances, orderflow estimé, macro et niveaux. Tout est en français, à l'heure de Paris. Le site se met à jour tout seul, gratuitement, sans serveur.

Adresse : <https://geoffreygrandon-lang.github.io/xauusd-desk/>

Ce n'est pas un conseil financier. L'orderflow est une estimation calculée sur le volume tick Dukascopy (spot, côté bid). Ce n'est pas le carnet d'ordres centralisé, ni le flux de ton courtier CFD.

## Installer sur l'iPhone

1. Ouvre **Safari** (pas Chrome).
2. Va sur l'adresse du site.
3. Touche le bouton **Partager** (le carré avec la flèche).
4. Descends jusqu'à **Sur l'écran d'accueil**.
5. Touche **Ajouter**.

L'icône « Macro Desk » s'ouvre ensuite comme une appli. La page se rafraîchit toute seule chaque minute.

## Installer sur Android

1. Ouvre **Chrome**.
2. Va sur l'adresse du site.
3. Menu **⋮** → **Installer l'application** (ou **Ajouter à l'écran d'accueil**).

## Notifications sur le téléphone (ntfy)

Deux messages, en français. Quand tu touches la notification, le tableau de bord s'ouvre.

1. **Résumé de séance**, du lundi au vendredi, vers **06:55, 07:55, 13:55, 16:55 et 21:00** (heure de Paris, été comme hiver). Le titre ressemble à : `XAU/USD — Londres baissière → VENTE NY`. Il dit quelle séance vient de se terminer, son sens, le sens privilégié pour la suivante, et le signal orderflow.
2. **Changement de signal**, dès que l'orderflow passe de ACHETEUR à VENDEUR, à NEUTRE, ou l'inverse. Le titre ressemble à : `XAU/USD — signal VENDEUR`.

La toute première mise à jour retient le signal **sans** t'écrire, pour éviter une fausse alerte.

### Brancher ntfy (une fois)

Le sujet ntfy est comme un mot de passe : quelqu'un qui le connaît peut lire tes alertes. Choisis un nom long et garde-le pour toi. Lettres, chiffres, tirets et `_` seulement, entre 3 et 64 caractères. Exemple : `xau-geo-k7m2p9qx4`.

1. Installe l'application **ntfy** (App Store, Play Store ou F-Droid).
2. Sur GitHub, ouvre ce dépôt → **Settings** → **Secrets and variables** → **Actions**.
3. Onglet **Secrets** → **New repository secret**.
   - Name : `NTFY_TOPIC`
   - Secret : le nom que tu as choisi
4. Dans l'app ntfy : **+** → abonne-toi à ce nom, serveur `ntfy.sh`.
5. Pour essayer tout de suite : onglet **Actions** → **Mise à jour des données** → **Run workflow** → mode `full`, notifier `oui`.

Sans ce secret, le site fonctionne quand même. Il n'envoie juste pas de notification.

## Ce qui se met à jour tout seul

En semaine, un robot GitHub :

- télécharge les bougies horaires XAU/USD chez Dukascopy (une heure à la fois, avec des pauses, pour ne pas se faire bloquer) ;
- recalcule séances, orderflow, RSI, moyennes, niveaux, brief et alertes ;
- met à jour le dollar (DXY), le VIX et le taux US 10 ans chez Yahoo, et les taux réels, le point mort d'inflation et le taux Fed chez FRED ;
- publie le site.

Vers les cinq heures ci-dessus, c'est une mise à jour complète **et** le résumé sur le téléphone. Entre les deux, un contrôle toutes les heures (environ 07:05–21:05, heure de Paris) ne prévient que si le signal orderflow a changé.

Le week-end, le marché est fermé : rien ne bouge, la dernière séance de vendredi reste affichée.

## Compléter Fed, banques centrales, géopolitique

Ces trois lignes ne peuvent pas être devinées. Tant que tu ne les remplis pas, l'app affiche **Indisponible** — elle n'invente pas un avis.

Édite `data/macro.json` sur GitHub (crayon en haut à droite du fichier). Exemple :

```json
{
  "derniere_mise_a_jour": "2026-10-30",
  "fed": {
    "impact": "neutre",
    "libelle": "Fed en pause",
    "note": "Pas de baisse annoncée cette semaine"
  },
  "banques_centrales": {
    "impact": "positif",
    "libelle": "Accumulation",
    "note": "Achats officiels toujours élevés"
  },
  "geopolitique": {
    "impact": "positif",
    "libelle": "Tensions",
    "note": "Risque géopolitique élevé"
  }
}
```

`impact` vaut `positif`, `neutre` ou `negatif`. Mets la date du jour dans `derniere_mise_a_jour`.

Le fichier sert seulement si tu veux forcer un commentaire. Fed, banques centrales et géopolitique sont remplis tout seuls à partir du calendrier Forex Factory (un appel par mise à jour, mis en cache). Sans chiffre « actual » publié, le score reste 0 et l'impact Neutre : rien n'est inventé. La géopolitique est un proxy VIX + or/dollar, indiqué comme tel.

## Lire l'écran

- **Sens privilégié** : tu trades dans le sens de la dernière séance terminée. Haussière → ACHAT, baissière → VENTE, neutre → ATTENDRE.
- **Journée** : 23h00 la veille → 22h00. Asie 23h00–09h00, Londres 09h00–15h30, New York 14h30–22h00. La semaine part de l'ouverture du dimanche.
- **Niveaux clés** : VWAP du jour et VWAP New York en tête, puis offre (au-dessus) et demande (en dessous). **Heatmap** : profil de volume minute, POC, zone de valeur, et X % au-dessus du prix.
- **Brief IA** et **gameplan** sont des textes automatiques à partir de ces règles, pas un avis personnalisé.
- **Zone neutre** : si l'orderflow est NEUTRE (écart achat/vente inférieur à 10 points), mieux vaut éviter de trader. Un RSI sous 30 ou au-dessus de 70 est aussi signalé.
- Les règles exactes (séances, orderflow, confiance, macro) sont en bas de l'appli.
- **Dernière MAJ** est l'heure de Paris de la dernière publication.

## Si quelque chose ne s'affiche pas

- « Indisponible » veut dire que la source n'a pas répondu, ou que la case n'est pas remplie. Ce n'est pas un zéro.
- Pas de notification : vérifie que le secret s'appelle exactement `NTFY_TOPIC`, et que l'app ntfy est abonnée au même nom sur `ntfy.sh`.
- Le site n'est pas encore public tant que GitHub Pages n'est pas activé. Une seule fois : ouvre le dépôt → **Settings** → **Pages** → **Build and deployment** → **Source** : **GitHub Actions**. Ensuite, onglet **Actions** → **Publier le site** → le dernier essai → **Re-run failed jobs**. L'adresse sera <https://geoffreygrandon-lang.github.io/xauusd-desk/>.
