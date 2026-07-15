# AS24 Sync

Ce dépôt automatise deux flux indépendants depuis le portail MyAS24 :

- les prix carburant quotidiens vers Supabase (`sync_as24.py`) ;
- toutes les transactions brutes de J-1 vers le module RK Trans d'Optimove
  Transport (`sync_transactions.py`).

## Transactions quotidiennes

Le workflow `AS24 Daily Transactions` s'exécute chaque jour à 06:20 UTC. Il :

1. ouvre une session MyAS24 avec Playwright ;
2. appelle l'API structurée
   `secured/transactions/getListTransactions` pour J-1, heure de Paris ;
3. conserve toutes les lignes sans filtrer l'offre, le produit ou l'unité ;
4. envoie les données par lots à l'API d'import Optimove.

Le payload AS24 original est transmis dans `raw_payload`. Quelques champs sont
aussi projetés (date, carte, immatriculation, produit, quantité, montants) pour
la recherche et l'affichage. L'identifiant AS24 rend chaque relance idempotente.

### Secrets GitHub requis

| Secret | Description |
|---|---|
| `AS24_CLIENT_ID` | Identifiant client MyAS24 |
| `AS24_USERNAME` | Utilisateur MyAS24 |
| `AS24_PASSWORD` | Mot de passe MyAS24 |
| `DASHDOC_AS24_IMPORT_URL` | URL complète de `/api/integrations/as24/transactions` |
| `DASHDOC_AS24_IMPORT_TOKEN` | Même secret que `AS24_IMPORT_TOKEN` côté Optimove |

Le workflow peut aussi être lancé manuellement avec une date `YYYY-MM-DD` et
une option `dry_run` qui récupère et valide AS24 sans envoyer les données.

### Backfill de l'historique

Pour importer une plage complète (jour par jour, upsert idempotent), lancer le
workflow avec `from_date` (et optionnellement `to_date`, J-1 par défaut), ou en
local :

```bash
python sync_transactions.py --from 2025-07-01 --to 2026-07-14
```

En cas d'échec en cours de route, le message d'erreur indique la commande
`--from … --to …` à relancer pour reprendre là où l'import s'est arrêté. La
session AS24 est renouvelée automatiquement si elle expire pendant la plage.

### Exécution locale

```bash
pip install -r requirements.txt
playwright install chromium
python -m unittest -v test_sync_transactions.py
python sync_transactions.py --date 2026-07-14 --dry-run
```

Sans `--date`, le script prend automatiquement J-1 dans le fuseau
`Europe/Paris`, y compris lors des changements d'heure. Attention : les péages
autoroutiers (PASSango/SANEF) peuvent apparaître dans l'extranet plusieurs
heures après la fin de la journée — c'est pour cela que le cron tourne le
lendemain matin et qu'un import lancé le soir même peut être incomplet.

## Prix carburant

Le workflow historique `AS24 Price Sync` continue d'exécuter `sync_as24.py`
tous les jours à 06:00 UTC. Il utilise, en plus des identifiants AS24 :

| Secret | Description |
|---|---|
| `SUPABASE_URL` | URL du projet Supabase de destination |
| `SUPABASE_SERVICE_KEY` | Clé service role Supabase |

## Sécurité

Les logs ne contiennent ni cookie JWT, ni mot de passe, ni payload brut. Les
transactions complètes sont conservées uniquement dans la base RK Trans.

## Licence

Usage privé.
