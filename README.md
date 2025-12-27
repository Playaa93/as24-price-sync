# AS24 Price Sync → FleetZen

Synchronisation automatique des prix carburant AS24 vers l'application FleetZen.

## Fonctionnement

```
GitHub Actions (7h00) → Script Python → AS24 API → FleetZen API
```

## Stations surveillées

| Station AS24 | Produit AS24 | Type FleetZen |
|--------------|--------------|---------------|
| AIRE DE GALANDE | Gazole | Diesel |
| AIRE DE GALANDE | AD Blue | AdBlue |
| MITRY MORY | GNR | GNR |

## Configuration

### Secrets GitHub requis

Dans Settings → Secrets and variables → Actions :

| Secret | Description |
|--------|-------------|
| `AS24_CLIENT_ID` | Numéro client AS24 |
| `AS24_USERNAME` | Email AS24 |
| `AS24_PASSWORD` | Mot de passe AS24 |
| `FLEETZEN_API_URL` | URL de l'app FleetZen (ex: https://fleetzen.vercel.app) |
| `FLEETZEN_API_KEY` | Clé API pour l'authentification |

### Variable d'environnement FleetZen

Ajouter dans Vercel :
```
FLEETZEN_API_KEY=votre_cle_api_secrete
```

## Déclenchement manuel

1. Aller dans Actions → AS24 Price Sync
2. Cliquer sur "Run workflow"

## Logs

Les logs de chaque exécution sont disponibles dans :
- Actions → Runs → Logs
- Artifacts (fichier sync.log)

## Test local

```bash
export AS24_CLIENT_ID="..."
export AS24_USERNAME="..."
export AS24_PASSWORD="..."
export FLEETZEN_API_URL="http://localhost:3000"
export FLEETZEN_API_KEY="..."

pip install requests playwright
playwright install chromium
python sync_as24.py
```
