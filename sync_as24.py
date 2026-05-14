#!/usr/bin/env python3
"""
Synchronisation automatique des prix AS24 vers Supabase.
"""

import os
import sys
import logging
from datetime import datetime
from playwright.sync_api import sync_playwright
import requests
from supabase import create_client, Client

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    handlers=[
        logging.FileHandler('sync.log'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

AS24_CLIENT_ID = os.environ.get('AS24_CLIENT_ID', '')
AS24_USERNAME = os.environ.get('AS24_USERNAME', '')
AS24_PASSWORD = os.environ.get('AS24_PASSWORD', '')
SUPABASE_URL = os.environ.get('SUPABASE_URL', '')
SUPABASE_SERVICE_KEY = os.environ.get('SUPABASE_SERVICE_KEY', '')

STATION = "AIRE DE GALANDE"
PRODUCT_MAPPING = {
    "Gazole": "Diesel",
    "Gazole B7": "Diesel",
    "AD Blue": "AdBlue",
    "AdBlue": "AdBlue",
    "GNR": "GNR",
    "HVO": "HVO",
    "HVO100": "HVO",
    "SP95": "SP95",
    "SP95-E10": "E10",
    "SP98": "SP98",
}


def get_as24_prices() -> list[dict]:
    if not all([AS24_CLIENT_ID, AS24_USERNAME, AS24_PASSWORD]):
        raise ValueError("Credentials AS24 manquants")

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

        log.info(f"URL après connexion: {page.url}")

        cookies = context.cookies()
        jwt_cookie = next((c for c in cookies if c['name'] == 'MYAS24-JWT'), None)

        if not jwt_cookie:
            page.screenshot(path='login_error.png')
            raise Exception("Cookie JWT non trouvé")

        jwt_token = jwt_cookie['value']
        log.info("Token JWT récupéré")
        browser.close()

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

    station_upper = STATION.upper()
    mapping_upper = {k.upper(): v for k, v in PRODUCT_MAPPING.items()}

    prices = []
    seen_fleetzen_types: set[str] = set()

    for item in data:
        if item.get('stationName', '').upper() != station_upper:
            continue

        product_name = item.get('productName', '')
        fleetzen_type = mapping_upper.get(product_name.upper())

        if not fleetzen_type:
            log.warning(f"  ⚠️  Produit non mappé chez {STATION}: {product_name!r} — ajoute-le dans PRODUCT_MAPPING")
            continue

        price_ht = item.get('localCurrencyPriceVATExcl', 0)
        if not price_ht or price_ht <= 0:
            continue

        if fleetzen_type in seen_fleetzen_types:
            log.warning(f"  ⚠️  {fleetzen_type} déjà rempli — produit ignoré: {product_name!r}")
            continue

        seen_fleetzen_types.add(fleetzen_type)
        prices.append({
            'fuel_type': fleetzen_type,
            'price_ht': price_ht,
            'price_ttc': round(price_ht * 1.20, 4),
        })
        log.info(f"  ✓ {STATION} - {product_name} → {fleetzen_type}: {price_ht}€/L HT")

    return prices


def save_to_supabase(prices: list[dict]) -> dict:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise ValueError("Configuration Supabase manquante")

    log.info("Connexion à Supabase...")
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

    results = {'success': 0, 'errors': [], 'skipped': 0}
    now = datetime.now()
    today_start = datetime(now.year, now.month, now.day, 0, 0, 0).isoformat()
    today_end = datetime(now.year, now.month, now.day, 23, 59, 59).isoformat()

    for price in prices:
        try:
            fuel_type = price['fuel_type']
            price_ttc = price['price_ttc']

            # Vérifier si un prix existe déjà pour aujourd'hui
            existing_today = supabase.table('fuel_prices').select('id, price_per_liter').eq(
                'fuel_type', fuel_type
            ).gte('effective_from', today_start).lte('effective_from', today_end).execute()

            if existing_today.data:
                current_price = existing_today.data[0].get('price_per_liter')
                if current_price and abs(float(current_price) - price_ttc) < 0.0001:
                    log.info(f"  = {fuel_type}: {price_ttc}€/L TTC (déjà présent)")
                    results['skipped'] += 1
                else:
                    supabase.table('fuel_prices').update({
                        'price_per_liter': price_ttc,
                        'updated_at': now.isoformat(),
                    }).eq('id', existing_today.data[0]['id']).execute()
                    log.info(f"  ↻ {fuel_type}: {current_price}€ → {price_ttc}€/L TTC (MAJ)")
                    results['success'] += 1
            else:
                # 1. Fermer l'ancien prix actif (comme backfill)
                supabase.table('fuel_prices').update({
                    'is_active': False,
                    'effective_until': today_start,
                }).eq('fuel_type', fuel_type).eq('is_active', True).execute()

                # 2. Insérer le nouveau prix du jour
                supabase.table('fuel_prices').insert({
                    'fuel_type': fuel_type,
                    'price_per_liter': price_ttc,
                    'is_active': True,
                    'effective_from': today_start,
                    'effective_until': None,
                    'created_at': now.isoformat(),
                    'updated_at': now.isoformat(),
                }).execute()
                log.info(f"  ✓ {fuel_type}: {price_ttc}€/L TTC (nouveau jour)")
                results['success'] += 1

        except Exception as e:
            error = f"{price['fuel_type']}: {str(e)}"
            results['errors'].append(error)
            log.error(f"  ✗ {error}")

    return results


def main():
    log.info("=" * 50)
    log.info("SYNC AS24 → SUPABASE")
    log.info("=" * 50)

    try:
        prices = get_as24_prices()

        if not prices:
            log.error("Aucun prix récupéré!")
            return 1

        log.info(f"\n{len(prices)} prix récupérés")

        results = save_to_supabase(prices)

        log.info("\n" + "=" * 50)
        log.info(f"Nouveaux/MAJ: {results['success']} | Déjà présents: {results['skipped']}")

        if results['errors']:
            log.error(f"Erreurs: {len(results['errors'])}")
            for error in results['errors']:
                log.error(f"  - {error}")
            return 1

        log.info("Sync terminé!")
        return 0

    except Exception as e:
        log.error(f"ERREUR: {e}")
        return 1


if __name__ == '__main__':
    sys.exit(main())
