# Macro Desk — version cloud gratuite

Le desk se met à jour tout seul à **7h, 12h30 et 22h** (heure de Casablanca), sans ton PC et sans crédits Claude :

- **GitHub Actions** (gratuit) lance le script ;
- l'**API YouTube** (gratuite) liste les nouveaux replays de Bloomberg Television ;
- **Gemini** (offre gratuite) regarde chaque vidéo et en extrait les idées clés ;
- le dashboard est publié sur **GitHub Pages** (gratuit), consultable sur PC et téléphone.

Installation : environ 20 minutes, une seule fois.

---

## Étape 1 — Clé Gemini (2 min)

1. Va sur **https://aistudio.google.com/apikey** et connecte-toi avec ton compte Google.
2. Clique sur **Create API key**, puis copie la clé (elle commence par `AIza…`).
3. Garde-la de côté pour l'étape 4.

> Les quotas gratuits par modèle sont visibles dans AI Studio (menu *Usage / Rate limits*).

## Étape 2 — Clé YouTube (5 min)

1. Va sur **https://console.cloud.google.com/** (même compte Google).
2. En haut, sélectionne le projet créé automatiquement par AI Studio (ou crée-en un).
3. Menu **API et services → Bibliothèque** → cherche **YouTube Data API v3** → **Activer**.
4. **API et services → Identifiants → Créer des identifiants → Clé API**. Copie-la.
5. (Conseillé) Clique sur la clé → *Restrictions d'API* → coche seulement **YouTube Data API v3** → Enregistrer.

## Étape 3 — Créer le dépôt GitHub (5 min)

1. Crée un compte sur **https://github.com** si besoin.
2. En haut à droite **+ → New repository** :
   - Nom : `macro-desk`
   - Visibilité : **Public** (obligatoire pour que GitHub Pages soit gratuit — seul le dashboard de résumés est visible, tes clés restent secrètes)
   - Clique **Create repository**.
3. Sur la page du dépôt vide, clique **uploading an existing file**.
4. Dézippe `macro-desk.zip` sur ton PC, ouvre le dossier `macro-desk`, sélectionne **tout son contenu** (y compris le dossier `.github`) et glisse-le dans la page.
   - Si Windows masque le dossier `.github` : Explorateur → *Affichage → Afficher → Éléments masqués*.
5. Clique **Commit changes**.

Vérifie que tu vois bien `.github/workflows/macro-desk.yml` dans le dépôt. S'il manque : *Add file → Create new file*, tape `.github/workflows/macro-desk.yml` comme nom, colle le contenu du fichier, et valide.

## Étape 4 — Ajouter les clés en secret (2 min)

Dans le dépôt : **Settings → Secrets and variables → Actions → New repository secret**

| Name | Secret |
|---|---|
| `GEMINI_API_KEY` | ta clé Gemini |
| `YOUTUBE_API_KEY` | ta clé YouTube |

Les noms doivent être écrits exactement ainsi.

## Étape 5 — Autoriser l'écriture et publier le dashboard (2 min)

1. **Settings → Actions → General** → en bas, *Workflow permissions* : coche **Read and write permissions** → Save.
2. **Settings → Pages** → *Source* : **Deploy from a branch** → Branch : `main`, dossier **`/docs`** → Save.
3. Ton dashboard sera à l'adresse : `https://<ton-pseudo>.github.io/macro-desk/`
   (mets-la en favori sur ton PC et ton téléphone).

## Étape 6 — Premier lancement

1. Onglet **Actions** → si GitHub demande d'activer les workflows, clique sur le bouton vert.
2. À gauche **Macro Desk** → **Run workflow** → **Run workflow**.
3. Compte 5 à 20 minutes selon le nombre de replays. Une coche verte = terminé.
4. Recharge ton dashboard 1 à 2 minutes plus tard.

---

## Utilisation au quotidien

- **Automatique** : rien à faire, le desk se met à jour 3 fois par jour.
- **Mise à jour immédiate** : bouton **⟳ Update now** du dashboard → **Run workflow**.
- **Analyser une vidéo précise** (même hors Bloomberg) : **⟳ Update now** → *Run workflow* → colle le lien dans « Lien YouTube à analyser » → Run.
- **Plusieurs liens** : bouton **+ Add video links** → colle un lien par ligne → *Commit changes*. Ils seront traités à la prochaine mise à jour.
- Sur téléphone, l'application **GitHub** permet de lancer *Run workflow*.

## Limites à connaître

- **Gratuit Gemini : 8 h de vidéo YouTube par jour.** Le script s'arrête à 7 h 30 (450 min) et garde le reste pour la mise à jour suivante. Les émissions de plus de 5 h (archives du direct 24/7) sont ignorées, sauf si tu les envoies toi-même ; pour toute vidéo très longue, seules les 160 premières minutes sont analysées.
- Les lives en cours ne sont pas traités : ils le seront une fois le replay publié.
- Sur l'offre gratuite, Google peut utiliser les données envoyées pour entraîner ses modèles : c'est sans enjeu pour des émissions publiques, mais n'y mets rien de confidentiel.
- GitHub peut lancer les mises à jour planifiées avec 5 à 30 min de retard.
- **Heures** : si les mises à jour arrivent 1 h trop tard, ouvre `.github/workflows/macro-desk.yml` et retire 1 h à chaque ligne `cron` (ex. `45 6` → `45 5`).

## Réglages (facultatif)

Dans `.github/workflows/macro-desk.yml`, section `env:` de l'étape *Update the desk*, tu peux ajouter :

| Variable | Défaut | Rôle |
|---|---|---|
| `GEMINI_MODELS` | `gemini-3-flash-preview,gemini-2.5-flash,gemini-2.5-flash-lite` | Modèles essayés dans l'ordre (bascule auto si quota atteint) |
| `MAX_MINUTES_PER_RUN` | `150` | Minutes de vidéo max par mise à jour |
| `DAILY_MINUTES_CAP` | `450` | Minutes de vidéo max par jour |
| `LOOKBACK_HOURS` | `36` | Ancienneté max des replays récupérés |
| `KEEP_DAYS` | `21` | Durée de conservation dans le dashboard |

## En cas de problème

- Le dashboard affiche un encadré jaune **Last run warnings** avec la dernière erreur.
- Le détail est dans **Actions → dernier run → Update the desk**.
- `Missing GEMINI_API_KEY…` → les secrets de l'étape 4 sont absents ou mal nommés.
- `403` côté YouTube → l'API YouTube Data v3 n'est pas activée sur le projet (étape 2.3).
- `All models failed` → quota Gemini du jour atteint ; ça reprend après minuit heure du Pacifique (vers 7h–8h du matin à Casablanca).
