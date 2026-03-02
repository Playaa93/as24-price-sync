#!/usr/bin/env python3
"""
Script de rattrapage historique des prix AS24.
Récupère les prix pour chaque jour du 18 novembre 2025 à aujourd'hui.
Utilise l'API REST Supabase directement (pas de dépendances complexes).
"""

import os
import sys
import json
import logging
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright
import requests

# Configuration logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    handlers=[
        logging.FileHandler('backfill.log'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# Configuration AS24
AS24_CLIENT_ID = os.environ.get('AS24_CLIENT_ID', '')
AS24_USERNAME = os.environ.get('AS24_USERNAME', '')
AS24_PASSWORD = os.environ.get('AS24_PASSWORD', '')

# Configuration Supabase
SUPABASE_URL = os.environ.get('SUPABASE_URL', '')
SUPABASE_SERVICE_KEY = os.environ.get('SUPABASE_SERVICE_KEY', '')

# Mapping AS24 → FleetZen
PRICE_MAPPING = [
    {"station": "AIRE DE GALANDE", "as24_product": "Gazole", "fleetzen_type": "Diesel"},
    {"station": "AIRE DE GALANDE", "as24_product": "AD Blue", "fleetzen_type": "AdBlue"},
    {"station": "MITRY MORY", "as24_product": "GNR", "fleetzen_type": "GNR"},
]


def supabase_request(method: str, table: str, data: dict = None, params: str = "") -> dict:
    """Fait une requête à l'API REST Supabase"""
    url = f"{SUPABASE_URL}/rest/v1/{table}{params}"
    headers = {
        'apikey': SUPABASE_SERVICE_KEY,
        'Authorization': f'Bearer {SUPABASE_SERVICE_KEY}',
        'Content-Type': 'application/json',
        'Prefer': 'return=minimal',
    }

    if method == 'GET':
        headers['Prefer'] = 'return=representation'
        response = requests.get(url, headers=headers)
    elif method == 'POST':
        response = requests.post(url, headers=headers, json=data)
    elif method == 'PATCH':
        response = requests.patch(url, headers=headers, json=data)
    elif method == 'DELETE':
        response = requests.delete(url, headers=headers)

    return response


def get_as24_token() -> str:
    """Se connecte à AS24 et récupère le JWT token"""
    log.info("Connexion à AS24...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        page.goto("https://extranet.as24.com/extranet/fr/login")

        try:
            page.click("button:has-text('Accepter & Fermer')", timeout=5000)
            page.wait_for_timeout(1000)
        except:
            pass

        page.wait_for_selector('input', timeout=10000)
        inputs = page.locator('input:visible').all()

        if len(inputs) >= 3:
            inputs[0].fill(AS24_CLIENT_ID)
            inputs[1].fill(AS24_USERNAME)
            inputs[2].fill(AS24_PASSWORD)
        else:
            raise Exception(f"Formulaire non trouvé - {len(inputs)} inputs")

        page.click('button:has-text("CONNEXION")')
        page.wait_for_timeout(3000)

        cookies = context.cookies()
        jwt_cookie = next((c for c in cookies if c['name'] == 'MYAS24-JWT'), None)

        if not jwt_cookie:
            raise Exception("Cookie JWT non trouvé")

        browser.close()
        return jwt_cookie['value']


def get_prices_for_date(jwt_token: str, target_date: datetime) -> list[dict]:
    """Récupère les prix AS24 pour une date donnée"""

    timestamp_ms = int(target_date.timestamp() * 1000)

    response = requests.post(
        'https://extranet.as24.com/myas24/secured/prices/getListPricesStations',
        headers={
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'Cookie': f'MYAS24-JWT={jwt_token}',
        },
        json={
            'applicationDate': timestamp_ms,
            'clientCurrency': 'EUR',
            'countriesId': ['FRA'],
        }
    )

    if response.status_code != 200:
        log.warning(f"Erreur API AS24 pour {target_date.date()}: {response.status_code}")
        return []

    data = response.json()

    prices = []
    for mapping in PRICE_MAPPING:
        for item in data:
            station_name = item.get('stationName', '').upper()
            product_name = item.get('productName', '').upper()

            if (station_name == mapping['station'].upper() and
                product_name == mapping['as24_product'].upper()):

                price_ht = item.get('localCurrencyPriceVATExcl', 0)
                if price_ht and price_ht > 0:
                    prices.append({
                        'fuel_type': mapping['fleetzen_type'],
                        'price_ht': price_ht,
                        'price_ttc': round(price_ht * 1.20, 4),  # 4 décimales
                    })
                break

    return prices


def main():
    log.info("=" * 60)
    log.info("RATTRAPAGE HISTORIQUE AS24 → SUPABASE")
    log.info("=" * 60)

    # Dates - rattrapage des jours manquants (workflow cassé du 24/02 au 01/03)
    start_date = datetime(2026, 2, 24)
    end_date = datetime(2026, 3, 1)

    log.info(f"Période: {start_date.date()} → {end_date.date()}")

    # Récupérer le token AS24
    jwt_token = get_as24_token()
    log.info("Token AS24 récupéré")

    # Parcourir chaque jour
    current_date = start_date
    total_inserted = 0
    last_prices = {}  # Pour tracker les changements de prix

    while current_date <= end_date:
        date_str = current_date.strftime('%d/%m/%Y')

        prices = get_prices_for_date(jwt_token, current_date)

        if not prices:
            log.info(f"📅 {date_str} - Aucun prix")
            current_date += timedelta(days=1)
            continue

        changes = []
        for price in prices:
            fuel_type = price['fuel_type']
            price_ttc = price['price_ttc']

            # Vérifier si un prix existe déjà pour ce jour
            day_start = current_date.strftime('%Y-%m-%dT00:00:00')
            day_end = current_date.strftime('%Y-%m-%dT23:59:59')
            existing = supabase_request('GET', 'fuel_prices',
                params=f'?fuel_type=eq.{fuel_type}&effective_from=gte.{day_start}&effective_from=lte.{day_end}&select=id')
            if existing.status_code == 200 and existing.json():
                log.info(f"  ⏭ {fuel_type} - déjà présent pour {date_str}")
                continue

            # Fermer l'ancien prix actif
            supabase_request('PATCH', 'fuel_prices',
                data={
                    'is_active': False,
                    'effective_until': current_date.isoformat(),
                },
                params=f'?fuel_type=eq.{fuel_type}&is_active=eq.true'
            )

            # Insérer le nouveau prix
            supabase_request('POST', 'fuel_prices',
                data={
                    'fuel_type': fuel_type,
                    'price_per_liter': price_ttc,
                    'is_active': True,
                    'effective_from': current_date.isoformat(),
                    'effective_until': None,
                    'created_at': current_date.isoformat(),
                    'updated_at': current_date.isoformat(),
                }
            )

            total_inserted += 1
            changes.append(f"{fuel_type}={price_ttc}€")

        log.info(f"📅 {date_str} - {', '.join(changes)}")

        current_date += timedelta(days=1)

    log.info("\n" + "=" * 60)
    log.info("RÉSUMÉ")
    log.info("=" * 60)
    log.info(f"Entrées insérées: {total_inserted}")
    log.info("Rattrapage terminé!")

    return 0


if __name__ == '__main__':
    sys.exit(main())
