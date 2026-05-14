#!/usr/bin/env python3
"""
Rattrapage historique des prix AS24 → Supabase.
Couvre mars 2025 → novembre 2025 (avant le cron daily).

Usage:
  python backfill_history.py --all                    # Tous les mois manquants
  python backfill_history.py --month 2025-03          # Un mois spécifique
  python backfill_history.py --month 2025-06 2025-07  # Plusieurs mois
  python backfill_history.py --dry-run --all          # Simuler sans écrire
"""

import os
import sys
import json
import time
import argparse
import logging
from datetime import datetime, timedelta
from calendar import monthrange
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

# Mapping AS24 → FleetZen (aligné avec sync_as24.py)
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
# Ancienne station MITRY MORY conservée pour la rétro-compatibilité du backfill
EXTRA_STATIONS = [
    {"station": "MITRY MORY", "as24_product": "GNR", "fleetzen_type": "GNR"},
]

# Mois à rattraper (avant le cron daily du 18/11/2025)
ALL_MONTHS = [
    "2025-03", "2025-04", "2025-05", "2025-06",
    "2025-07", "2025-08", "2025-09", "2025-10", "2025-11",
]

# Pause entre chaque requête API AS24 (secondes) pour éviter le rate-limit
API_DELAY = 1.5


def supabase_request(method: str, table: str, data: dict = None, params: str = ""):
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
        return requests.get(url, headers=headers)
    elif method == 'POST':
        return requests.post(url, headers=headers, json=data)
    elif method == 'PATCH':
        return requests.patch(url, headers=headers, json=data)
    elif method == 'DELETE':
        return requests.delete(url, headers=headers)


def get_as24_token() -> str:
    """Se connecte à AS24 et récupère le JWT token"""
    if not all([AS24_CLIENT_ID, AS24_USERNAME, AS24_PASSWORD]):
        raise ValueError("Credentials AS24 manquants (AS24_CLIENT_ID, AS24_USERNAME, AS24_PASSWORD)")

    log.info("🔑 Connexion à AS24...")

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
            page.screenshot(path='login_error.png')
            raise Exception(f"Formulaire non trouvé - {len(inputs)} inputs")

        page.click('button:has-text("CONNEXION")')
        page.wait_for_timeout(3000)

        cookies = context.cookies()
        jwt_cookie = next((c for c in cookies if c['name'] == 'MYAS24-JWT'), None)

        if not jwt_cookie:
            page.screenshot(path='login_error.png')
            raise Exception("Cookie JWT non trouvé — screenshot sauvé dans login_error.png")

        browser.close()
        log.info("🔑 Token JWT récupéré")
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

    if response.status_code == 401:
        raise TokenExpiredError("JWT expiré")

    if response.status_code != 200:
        log.warning(f"  Erreur API AS24 pour {target_date.date()}: HTTP {response.status_code}")
        return []

    data = response.json()

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
            continue

        price_ht = item.get('localCurrencyPriceVATExcl', 0)
        if not price_ht or price_ht <= 0:
            continue

        if fleetzen_type in seen_fleetzen_types:
            continue
        seen_fleetzen_types.add(fleetzen_type)

        prices.append({
            'fuel_type': fleetzen_type,
            'price_ht': price_ht,
            'price_ttc': round(price_ht * 1.20, 4),
        })

    for extra in EXTRA_STATIONS:
        for item in data:
            if (item.get('stationName', '').upper() == extra['station'].upper() and
                item.get('productName', '').upper() == extra['as24_product'].upper()):
                price_ht = item.get('localCurrencyPriceVATExcl', 0)
                if price_ht and price_ht > 0 and extra['fleetzen_type'] not in seen_fleetzen_types:
                    seen_fleetzen_types.add(extra['fleetzen_type'])
                    prices.append({
                        'fuel_type': extra['fleetzen_type'],
                        'price_ht': price_ht,
                        'price_ttc': round(price_ht * 1.20, 4),
                    })
                break

    return prices


class TokenExpiredError(Exception):
    pass


def get_month_range(month_str: str) -> tuple[datetime, datetime]:
    """Retourne (premier_jour, dernier_jour) pour un mois 'YYYY-MM'"""
    year, month = map(int, month_str.split('-'))
    first_day = datetime(year, month, 1)
    last_day_num = monthrange(year, month)[1]
    last_day = datetime(year, month, last_day_num)
    # Ne pas dépasser le 17/11/2025 (début du cron daily)
    cutoff = datetime(2025, 11, 17)
    if last_day > cutoff:
        last_day = cutoff
    return first_day, last_day


def process_month(month_str: str, jwt_token: str, dry_run: bool = False) -> dict:
    """Traite un mois complet. Retourne les stats."""
    first_day, last_day = get_month_range(month_str)
    total_days = (last_day - first_day).days + 1

    log.info(f"\n{'=' * 60}")
    log.info(f"📅 MOIS: {month_str} ({first_day.date()} → {last_day.date()}, {total_days} jours)")
    log.info(f"{'=' * 60}")

    stats = {'inserted': 0, 'skipped': 0, 'existing': 0, 'errors': 0, 'no_data': 0}
    current_date = first_day

    while current_date <= last_day:
        date_str = current_date.strftime('%d/%m/%Y')

        try:
            prices = get_prices_for_date(jwt_token, current_date)
        except TokenExpiredError:
            log.warning("⚠️  Token expiré, re-authentification...")
            jwt_token = get_as24_token()
            prices = get_prices_for_date(jwt_token, current_date)

        if not prices:
            log.info(f"  {date_str} - Aucun prix (API vide)")
            stats['no_data'] += 1
            current_date += timedelta(days=1)
            time.sleep(API_DELAY)
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
                stats['existing'] += 1
                continue

            if dry_run:
                changes.append(f"{fuel_type}={price_ttc}€")
                stats['inserted'] += 1
                continue

            # Insérer le nouveau prix (pas de is_active pour le backfill historique)
            result = supabase_request('POST', 'fuel_prices',
                data={
                    'fuel_type': fuel_type,
                    'price_per_liter': price_ttc,
                    'is_active': False,
                    'effective_from': current_date.isoformat(),
                    'effective_until': (current_date + timedelta(days=1)).isoformat(),
                    'created_at': current_date.isoformat(),
                    'updated_at': current_date.isoformat(),
                }
            )

            if result.status_code in (200, 201):
                stats['inserted'] += 1
                changes.append(f"{fuel_type}={price_ttc}€")
            else:
                stats['errors'] += 1
                log.error(f"  ✗ {fuel_type} {date_str}: HTTP {result.status_code} - {result.text}")

        if changes:
            prefix = "[DRY-RUN] " if dry_run else ""
            log.info(f"  {prefix}{date_str} - {', '.join(changes)}")
        elif stats['existing'] > 0:
            pass  # Silencieux pour les jours déjà présents

        current_date += timedelta(days=1)
        time.sleep(API_DELAY)

    return stats, jwt_token


def main():
    parser = argparse.ArgumentParser(description='Rattrapage historique AS24 → Supabase')
    parser.add_argument('--all', action='store_true', help='Rattraper tous les mois manquants (mars→nov 2025)')
    parser.add_argument('--month', nargs='+', help='Mois spécifiques (ex: 2025-03 2025-04)')
    parser.add_argument('--dry-run', action='store_true', help='Simuler sans écrire en base')
    args = parser.parse_args()

    if not args.all and not args.month:
        parser.print_help()
        print("\nExemples:")
        print("  python backfill_history.py --all")
        print("  python backfill_history.py --month 2025-03")
        print("  python backfill_history.py --month 2025-06 2025-07 --dry-run")
        return 1

    months_to_process = ALL_MONTHS if args.all else args.month

    # Valider les mois
    for m in months_to_process:
        try:
            datetime.strptime(m, '%Y-%m')
        except ValueError:
            log.error(f"Format invalide: {m} (attendu: YYYY-MM)")
            return 1

    log.info("=" * 60)
    log.info("RATTRAPAGE HISTORIQUE AS24 → SUPABASE")
    log.info(f"Mois: {', '.join(months_to_process)}")
    if args.dry_run:
        log.info("🔶 MODE DRY-RUN — aucune écriture en base")
    log.info("=" * 60)

    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        log.error("Variables SUPABASE_URL et SUPABASE_SERVICE_KEY requises")
        return 1

    # Récupérer le token AS24 (sera renouvelé entre les mois si nécessaire)
    jwt_token = get_as24_token()

    grand_total = {'inserted': 0, 'skipped': 0, 'existing': 0, 'errors': 0, 'no_data': 0}

    for i, month in enumerate(months_to_process):
        # Re-auth entre chaque mois (le JWT peut expirer)
        if i > 0:
            log.info("\n🔄 Re-authentification avant le mois suivant...")
            try:
                jwt_token = get_as24_token()
            except Exception as e:
                log.error(f"Echec re-auth: {e}")
                log.info("⏸  Pause 30s avant retry...")
                time.sleep(30)
                jwt_token = get_as24_token()

        try:
            stats, jwt_token = process_month(month, jwt_token, dry_run=args.dry_run)
            for key in grand_total:
                grand_total[key] += stats.get(key, 0)

            log.info(f"\n  → {month}: +{stats['inserted']} insérés | "
                     f"{stats['existing']} déjà présents | "
                     f"{stats['no_data']} jours sans données | "
                     f"{stats['errors']} erreurs")

        except Exception as e:
            log.error(f"✗ Echec pour {month}: {e}")
            grand_total['errors'] += 1
            continue

    # Résumé final
    log.info("\n" + "=" * 60)
    log.info("RÉSUMÉ GLOBAL")
    log.info("=" * 60)
    log.info(f"  Insérés:        {grand_total['inserted']}")
    log.info(f"  Déjà présents:  {grand_total['existing']}")
    log.info(f"  Jours sans data:{grand_total['no_data']}")
    log.info(f"  Erreurs:        {grand_total['errors']}")

    if args.dry_run:
        log.info("\n🔶 C'était un DRY-RUN — rien n'a été écrit en base.")

    log.info("\nRattrapage terminé!")
    return 0 if grand_total['errors'] == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
