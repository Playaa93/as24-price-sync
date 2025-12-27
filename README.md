# AS24 Price Sync

Synchronisation automatique des prix carburant AS24 vers une application externe.

## Fonctionnement

```
GitHub Actions (cron) → Script Python → AS24 API → API externe
```

## Configuration

### Secrets GitHub requis

| Secret | Description |
|--------|-------------|
| `AS24_CLIENT_ID` | Numéro client AS24 |
| `AS24_USERNAME` | Email AS24 |
| `AS24_PASSWORD` | Mot de passe AS24 |
| `FLEETZEN_API_URL` | URL de l'API destination |
| `FLEETZEN_API_KEY` | Clé API pour l'authentification |

## Déclenchement

- **Automatique** : tous les jours à 7h (Paris)
- **Manuel** : Actions → Run workflow

## Licence

Usage privé.
