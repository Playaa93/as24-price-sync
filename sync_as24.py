#!/usr/bin/env python3
"""
Synchronisation automatique des prix AS24 vers API externe.
Configuration via variables d'environnement.
"""

import os
import sys
import json
import logging
from datetime import datetime
from playwright.sync_api import sync_playwright
import requests

# Configuration logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    handlers=[
        logging.FileHandler('sync.log'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# Configuration AS24
AS24_CLIENT_ID = os.environ.get('AS24_CLIENT_ID', '')
AS24_USERNAME = os.environ.get('AS24_USERNAME', '')
AS24_PASSWORD = os.environ.get('AS24_PASSWORD', '')

# Configuration FleetZen
FLEETZEN_API_URL = os.environ.get('FLEETZEN_API_URL', '')
FLEETZEN_API_KEY = os.environ.get('FLEETZEN_API_KEY', '')

# Mapping AS24 → FleetZen (JSON depuis variable d'environnement)
# Format: [{"station":"NOM","as24_product":"Produit","fleetzen_type":"Type"},...]
PRICE_MAPPING_JSON = os.environ.get('PRICE_MAPPING', '[]')
try:
    PRICE_MAPPING = json.loads(PRICE_MAPPING_JSON)
except json.JSONDecodeError:
    PRICE_MAPPING = []


def get_as24_prices() -> list[dict]:
    """
    Se connecte à AS24 et récupère les prix via l'API
    """
    if not all([AS24_CLIENT_ID, AS24_USERNAME, AS24_PASSWORD]):
        raise ValueError("Credentials AS24 manquants")

    log.info("Connexion à AS24...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        # Page de login
        page.goto("https://extranet.as24.com/extranet/fr/login")

        # Accepter cookies
        try:
            page.click("button:has-text('Accepter & Fermer')", timeout=5000)
            page.wait_for_timeout(1000)
        except:
            pass

        # Remplir formulaire
        page.wait_for_selector('input', timeout=10000)
        inputs = page.locator('input:visible').all()

        if len(inputs) >= 3:
            inputs[0].fill(AS24_CLIENT_ID)
            inputs[1].fill(AS24_USERNAME)
            inputs[2].fill(AS24_PASSWORD)
        else:
            raise Exception(f"Formulaire non trouvé - {len(inputs)} inputs")

        # Connexion
        page.click('button:has-text("CONNEXION")')
        page.wait_for_timeout(3000)

        log.info(f"URL après connexion: {page.url}")

        # Récupérer les cookies pour l'API
        cookies = context.cookies()
        jwt_cookie = next((c for c in cookies if c['name'] == 'MYAS24-JWT'), None)

        if not jwt_cookie:
            page.screenshot(path='login_error.png')
            raise Exception("Cookie JWT non trouvé")

        jwt_token = jwt_cookie['value']
        log.info("Token JWT récupéré")

        browser.close()

    # Appeler l'API AS24 pour récupérer les prix
    log.info("Récupération des prix via API AS24...")

    response = requests.post(
        'https://extranet.as24.com/myas24/secured/prices/getListPricesStations',
        headers={
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'Cookie': f'MYAS24-JWT={jwt_token}',
        },
        json={
            'applicationDate': int(datetime.now().timestamp() * 1000),
            'clientCurrency': 'EUR',
            'countriesId': ['FRA'],
        }
    )

    if response.status_code != 200:
        raise Exception(f"Erreur API AS24: {response.status_code}")

    data = response.json()
    log.info(f"Données reçues: {len(data)} entrées")

    # Debug: afficher la structure des données
    if data:
        log.info(f"Clés disponibles: {list(data[0].keys()) if isinstance(data[0], dict) else 'pas un dict'}")
        log.info(f"Premier élément: {data[0]}")
        log.info(f"Mapping configuré: {PRICE_MAPPING}")

    # Extraire les prix configurés
    prices = []
    for mapping in PRICE_MAPPING:
        for item in data:
            station_name = item.get('Nom de la station', '').upper()
            product_name = item.get('Produit', '').upper()

            if (station_name == mapping['station'].upper() and
                product_name == mapping['as24_product'].upper()):

                # Parser le prix HT
                price_str = item.get('HT barême en devise locale', '')
                price_match = price_str.replace('€/L', '').replace(',', '.').strip()

                try:
                    price_ht = float(price_match)
                except ValueError:
                    log.warning(f"Prix invalide: {price_str}")
                    continue

                prices.append({
                    'station': mapping['station'],
                    'as24_product': mapping['as24_product'],
                    'fleetzen_type': mapping['fleetzen_type'],
                    'price_ht': price_ht,
                    'date': item.get("Date d'application", datetime.now().strftime('%Y-%m-%d')),
                })
                log.info(f"  ✓ {mapping['station']} - {mapping['as24_product']}: {price_ht}€/L")
                break

    return prices


def send_to_fleetzen(prices: list[dict]) -> dict:
    """
    Envoie les prix à l'API FleetZen
    """
    if not FLEETZEN_API_URL or not FLEETZEN_API_KEY:
        raise ValueError("Configuration FleetZen manquante")

    log.info(f"Envoi de {len(prices)} prix à FleetZen...")

    results = {'success': 0, 'errors': []}

    for price in prices:
        try:
            response = requests.post(
                f"{FLEETZEN_API_URL}/api/cron/as24-sync",
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {FLEETZEN_API_KEY}',
                },
                json={
                    'fuel_type': price['fleetzen_type'],
                    'prix_achat_as24': price['price_ht'],
                    'effective_from': price['date'],
                    'source': 'as24_sync',
                    'notes': f"AS24 - {price['station']} {price['as24_product']}",
                }
            )

            if response.status_code == 200:
                results['success'] += 1
                log.info(f"  ✓ {price['fleetzen_type']}: {price['price_ht']}€/L")
            else:
                error = f"{price['fleetzen_type']}: {response.status_code} - {response.text}"
                results['errors'].append(error)
                log.error(f"  ✗ {error}")

        except Exception as e:
            error = f"{price['fleetzen_type']}: {str(e)}"
            results['errors'].append(error)
            log.error(f"  ✗ {error}")

    return results


def main():
    log.info("=" * 50)
    log.info("DÉMARRAGE SYNC AS24 → FLEETZEN")
    log.info("=" * 50)

    try:
        # 1. Récupérer les prix AS24
        prices = get_as24_prices()

        if not prices:
            log.error("Aucun prix récupéré!")
            return 1

        log.info(f"\n{len(prices)} prix récupérés")

        # 2. Envoyer à FleetZen
        results = send_to_fleetzen(prices)

        # 3. Résumé
        log.info("\n" + "=" * 50)
        log.info("RÉSUMÉ")
        log.info("=" * 50)
        log.info(f"Prix mis à jour: {results['success']}/{len(prices)}")

        if results['errors']:
            log.error(f"Erreurs: {len(results['errors'])}")
            for error in results['errors']:
                log.error(f"  - {error}")
            return 1

        log.info("Sync terminé avec succès!")
        return 0

    except Exception as e:
        log.error(f"ERREUR FATALE: {e}")
        return 1


if __name__ == '__main__':
    sys.exit(main())
