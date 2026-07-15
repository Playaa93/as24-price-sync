#!/usr/bin/env python3
"""Import quotidien des transactions AS24 brutes vers Optimove Transport."""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time as time_module
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo


LOGIN_URL = "https://extranet.as24.com/extranet/fr/login"
TRANSACTIONS_URL = (
    "https://extranet.as24.com/myas24/secured/transactions/getListTransactions"
)
PARIS = ZoneInfo("Europe/Paris")
MAX_BATCH_ITEMS = 1_000
MAX_BATCH_BYTES = 1_500_000

log = logging.getLogger("as24.transactions")


class As24SyncError(RuntimeError):
    """Erreur contrôlée du flux de synchronisation."""


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s: %(message)s",
        handlers=[
            logging.FileHandler("transactions-sync.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise As24SyncError(f"Variable d'environnement manquante: {name}")
    return value


def _requests_module():
    try:
        import requests
    except ModuleNotFoundError as exc:
        raise As24SyncError("Dépendance Python manquante: requests") from exc
    return requests


def parse_target_date(raw: str | None, now: datetime | None = None) -> date:
    if raw:
        try:
            return date.fromisoformat(raw)
        except ValueError as exc:
            raise As24SyncError("--date doit être au format YYYY-MM-DD") from exc
    paris_now = (now or datetime.now(tz=PARIS)).astimezone(PARIS)
    return paris_now.date() - timedelta(days=1)


def parse_target_days(
    raw_date: str | None,
    raw_from: str | None,
    raw_to: str | None,
    now: datetime | None = None,
) -> list[date]:
    if raw_from is None and raw_to is not None:
        raise As24SyncError("--to nécessite --from")
    if raw_from is None:
        return [parse_target_date(raw_date, now)]
    if raw_date is not None:
        raise As24SyncError("--date et --from sont mutuellement exclusifs")
    try:
        start = date.fromisoformat(raw_from)
    except ValueError as exc:
        raise As24SyncError("--from doit être au format YYYY-MM-DD") from exc
    if raw_to is None:
        end = parse_target_date(None, now)
    else:
        try:
            end = date.fromisoformat(raw_to)
        except ValueError as exc:
            raise As24SyncError("--to doit être au format YYYY-MM-DD") from exc
    if start > end:
        raise As24SyncError("--from doit précéder --to")
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def paris_day_bounds_ms(target: date) -> tuple[int, int]:
    start = datetime.combine(target, time.min, tzinfo=PARIS)
    end = datetime.combine(target + timedelta(days=1), time.min, tzinfo=PARIS)
    return (
        int(start.timestamp() * 1000),
        int(end.timestamp() * 1000) - 1,
    )


def build_as24_filter(target: date) -> dict[str, Any]:
    begin_date, end_date = paris_day_bounds_ms(target)
    return {
        "accountingDocumentId": "",
        "beginDate": begin_date,
        "countriesISO": [],
        "driverName": "",
        "endDate": end_date,
        "registration": "",
        "supportId": "",
        "supportOffers": [],
        "products": [],
        "cmv": "",
    }


def authenticate_as24(client_id: str, username: str, password: str) -> str:
    # Import différé pour permettre les tests unitaires sans navigateur installé.
    from playwright.sync_api import sync_playwright

    log.info("Connexion au portail AS24")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            page = context.new_page()
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)

            try:
                page.click("button:has-text('Accepter & Fermer')", timeout=5_000)
            except Exception:
                pass

            page.wait_for_selector("input:visible", timeout=20_000)
            inputs = page.locator("input:visible")
            if inputs.count() < 3:
                raise As24SyncError("Formulaire de connexion AS24 introuvable")

            inputs.nth(0).fill(client_id)
            inputs.nth(1).fill(username)
            inputs.nth(2).fill(password)
            page.click('button:has-text("CONNEXION")', timeout=10_000)

            jwt_token = ""
            for _ in range(20):
                jwt_cookie = next(
                    (
                        cookie
                        for cookie in context.cookies()
                        if cookie["name"] == "MYAS24-JWT"
                    ),
                    None,
                )
                if jwt_cookie:
                    jwt_token = jwt_cookie["value"]
                    break
                page.wait_for_timeout(500)

            if not jwt_token:
                raise As24SyncError("Connexion AS24 refusée: cookie JWT absent")
            log.info("Session AS24 obtenue")
            return jwt_token
        finally:
            browser.close()


def extract_transaction_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = None
        for key in ("transactions", "items", "content", "data"):
            candidate = payload.get(key)
            if isinstance(candidate, list):
                rows = candidate
                break
        if rows is None:
            raise As24SyncError("Format de réponse AS24 inconnu")
    else:
        raise As24SyncError("Format de réponse AS24 inconnu")

    if not all(isinstance(row, dict) for row in rows):
        raise As24SyncError("La réponse AS24 contient une ligne invalide")
    return rows


def fetch_as24_transactions(
    jwt_token: str,
    target: date,
    session: Any | None = None,
) -> list[dict[str, Any]]:
    requests = _requests_module()
    client = session or requests.Session()
    try:
        response = client.post(
            TRANSACTIONS_URL,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Cookie": f"MYAS24-JWT={jwt_token}",
            },
            json=build_as24_filter(target),
            timeout=90,
        )
    except requests.RequestException as exc:
        raise As24SyncError("API transactions AS24 injoignable") from exc
    if response.status_code != 200:
        raise As24SyncError(f"API transactions AS24: HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise As24SyncError("Réponse JSON AS24 illisible") from exc
    return extract_transaction_list(payload)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value or None


def _number(value: Any) -> float | int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = value
    elif isinstance(value, str) and value.strip():
        normalized = value.strip().replace(" ", "").replace(",", ".")
        try:
            parsed = float(normalized)
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(parsed):
        return None
    if float(parsed).is_integer():
        return int(parsed)
    return float(parsed)


def _iso_timestamp(value: Any) -> str:
    parsed: datetime | None = None
    numeric = _number(value)
    if numeric is not None:
        seconds = float(numeric) / 1000 if abs(float(numeric)) >= 100_000_000_000 else float(numeric)
        try:
            parsed = datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            parsed = None
    elif isinstance(value, str):
        candidate = value.strip()
        if candidate:
            try:
                parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
            except ValueError:
                parsed = None
    elif isinstance(value, list) and len(value) >= 3:
        try:
            parts = [int(part) for part in value[:7]]
            parts.extend([0] * (7 - len(parts)))
            parsed = datetime(*parts, tzinfo=PARIS)
        except (TypeError, ValueError):
            parsed = None

    if parsed is None:
        raise As24SyncError("transactionDate AS24 invalide")
    if parsed.year < 2000:
        # Un timestamp 0 ou tronqué donnerait une date 1970 silencieusement fausse.
        raise As24SyncError("transactionDate AS24 invalide")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=PARIS)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _transaction_timestamp(row: dict[str, Any]) -> str:
    for field in ("transactionDate", "exitTransactionDate"):
        value = row.get(field)
        if value is None:
            continue
        try:
            return _iso_timestamp(value)
        except As24SyncError:
            continue
    raise As24SyncError(
        "date de transaction AS24 invalide"
        f" (transactionDate={row.get('transactionDate')!r},"
        f" exitTransactionDate={row.get('exitTransactionDate')!r})"
    )


def _external_id(row: dict[str, Any]) -> str:
    transaction_id = _text(row.get("transactionId"))
    if transaction_id:
        return transaction_id
    transaction_number = _text(row.get("transactionNumber"))
    if transaction_number:
        return f"number:{transaction_number}"
    raise As24SyncError("Transaction AS24 sans identifiant stable")


def _card_reference(row: dict[str, Any]) -> str | None:
    support_id = _text(row.get("supportId"))
    extension = _text(row.get("supportExtension"))
    if support_id and extension:
        return f"{support_id}-{extension}"
    return support_id or extension


def _vehicle_registration(row: dict[str, Any]) -> str | None:
    vehicle = row.get("vehicle")
    if isinstance(vehicle, dict):
        registration = _text(vehicle.get("immatriculation"))
        if registration:
            return registration
    return _text(row.get("vehicleRegistration"))


def normalize_as24_transaction(row: dict[str, Any]) -> dict[str, Any]:
    quantity = _number(row.get("quantity"))
    client_amount_ht = _number(row.get("clientExcludingTaxAmount"))
    client_amount_ttc = _number(row.get("clientIncludingTaxAmount"))
    has_client_amount = any(
        amount is not None
        for amount in (client_amount_ht, client_amount_ttc)
    )
    local_amount_ht = _number(row.get("localExcludingTaxAmount"))
    local_vat = _number(row.get("localVatAmount"))
    local_amount_ttc = _number(row.get("localIncludingTaxAmount"))

    amount_ht = (
        client_amount_ht if has_client_amount else local_amount_ht
    )
    amount_ttc = (
        client_amount_ttc if has_client_amount else local_amount_ttc
    )
    currency = _text(
        row.get("clientCurrency") if has_client_amount else row.get("localCurrency")
    ) or "EUR"
    vat_amount = (
        float(amount_ttc) - float(amount_ht)
        if has_client_amount and amount_ttc is not None and amount_ht is not None
        else local_vat
    )

    unit_price_ht = _number(row.get("scaleExcludingTaxPrice"))
    if unit_price_ht is None and amount_ht is not None and quantity not in (None, 0):
        unit_price_ht = abs(float(amount_ht) / float(quantity))

    support_offer = _number(row.get("supportOffer"))
    if not isinstance(support_offer, int):
        support_offer = None

    return {
        "external_id": _external_id(row),
        "transaction_at": _transaction_timestamp(row),
        "card_reference": _card_reference(row),
        "vehicle_registration": _vehicle_registration(row),
        "station_name": _text(row.get("stationName")),
        "station_city": _text(row.get("stationCity") or row.get("stationTown")),
        "country_code": _text(row.get("stationCountry")),
        "product": _text(row.get("productLabel")),
        "support_offer": support_offer,
        "quantity": quantity,
        "unit": _text(row.get("unit")),
        "unit_price_ht": unit_price_ht,
        "amount_ht": amount_ht,
        "vat_amount": vat_amount,
        "amount_ttc": amount_ttc,
        "currency": currency.upper(),
        "raw_payload": row,
    }


def normalize_all_transactions(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        try:
            transaction = normalize_as24_transaction(row)
        except As24SyncError as exc:
            reference = row.get("transactionId") or row.get("transactionNumber")
            raise As24SyncError(
                f"Ligne AS24 {index} (id={reference!r}): {exc}"
            ) from exc
        external_id = transaction["external_id"]
        if external_id in normalized:
            if normalized[external_id]["raw_payload"] == transaction["raw_payload"]:
                log.warning(
                    "Transaction AS24 identique dupliquée dans la réponse: %s",
                    external_id,
                )
                continue
            raise As24SyncError(
                f"Identifiant AS24 {external_id} associé à deux payloads différents"
            )
        normalized[external_id] = transaction
    return list(normalized.values())


def transaction_batches(
    transactions: Iterable[dict[str, Any]],
    max_items: int = MAX_BATCH_ITEMS,
    max_bytes: int = MAX_BATCH_BYTES,
) -> Iterable[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    items_bytes = 0
    envelope_bytes = len(b'{"transactions":[') + len(b']}')
    for transaction in transactions:
        transaction_bytes = len(
            json.dumps(transaction, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        if envelope_bytes + transaction_bytes > max_bytes:
            raise As24SyncError("Une transaction brute dépasse la taille d'import maximale")
        candidate_bytes = (
            envelope_bytes + items_bytes + transaction_bytes + len(batch)
        )
        if batch and (len(batch) >= max_items or candidate_bytes > max_bytes):
            yield batch
            batch = []
            items_bytes = 0
        batch.append(transaction)
        items_bytes += transaction_bytes
    if batch:
        yield batch


def import_batch(
    destination_url: str,
    destination_token: str,
    transactions: list[dict[str, Any]],
    session: Any | None = None,
) -> dict[str, Any]:
    requests = _requests_module()
    client = session or requests.Session()
    try:
        response = client.post(
            destination_url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {destination_token}",
                "Content-Type": "application/json",
            },
            json={"transactions": transactions},
            timeout=90,
        )
    except requests.RequestException as exc:
        raise As24SyncError("API Optimove injoignable") from exc
    if response.status_code != 200:
        raise As24SyncError(f"API Optimove: HTTP {response.status_code}")
    try:
        result = response.json()
    except ValueError as exc:
        raise As24SyncError("Réponse JSON Optimove illisible") from exc
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise As24SyncError("Réponse Optimove invalide")
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        help="Jour AS24 à importer (YYYY-MM-DD). Par défaut: J-1, heure de Paris.",
    )
    parser.add_argument(
        "--from",
        dest="from_date",
        help="Début de plage à importer (YYYY-MM-DD), pour un backfill multi-jours.",
    )
    parser.add_argument(
        "--to",
        dest="to_date",
        help="Fin de plage à importer (YYYY-MM-DD). Par défaut: J-1, heure de Paris.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Récupère et valide les données sans les envoyer.",
    )
    return parser.parse_args(argv)


def _fetch_day_with_reauth(
    jwt_token: str,
    day: date,
    session: Any,
    reauthenticate: Any,
) -> tuple[list[dict[str, Any]], str]:
    try:
        return fetch_as24_transactions(jwt_token, day, session=session), jwt_token
    except As24SyncError as exc:
        if "HTTP 401" not in str(exc) and "HTTP 403" not in str(exc):
            raise
        log.info("Session AS24 expirée, reconnexion")
        jwt_token = reauthenticate()
        return fetch_as24_transactions(jwt_token, day, session=session), jwt_token


def run(args: argparse.Namespace) -> int:
    days = parse_target_days(args.date, args.from_date, args.to_date)
    client_id = require_env("AS24_CLIENT_ID")
    username = require_env("AS24_USERNAME")
    password = require_env("AS24_PASSWORD")

    if len(days) == 1:
        log.info("Synchronisation des transactions AS24 du %s", days[0].isoformat())
    else:
        log.info(
            "Backfill des transactions AS24 du %s au %s (%s jours)",
            days[0].isoformat(),
            days[-1].isoformat(),
            len(days),
        )

    destination_url = ""
    destination_token = ""
    if not args.dry_run:
        destination_url = require_env("DASHDOC_AS24_IMPORT_URL")
        destination_token = require_env("DASHDOC_AS24_IMPORT_TOKEN")

    def reauthenticate() -> str:
        return authenticate_as24(client_id, username, password)

    jwt_token = reauthenticate()
    requests = _requests_module()
    total_transactions = 0
    total_imported = 0
    total_batches = 0
    with requests.Session() as session:
        for index, day in enumerate(days):
            if index:
                time_module.sleep(0.3)
            try:
                raw_rows, jwt_token = _fetch_day_with_reauth(
                    jwt_token, day, session, reauthenticate
                )
                log.info(
                    "AS24 a retourné %s transaction(s) brute(s) pour le %s",
                    len(raw_rows),
                    day.isoformat(),
                )
                transactions = normalize_all_transactions(raw_rows)
                total_transactions += len(transactions)
                if not transactions or args.dry_run:
                    continue
                for batch in transaction_batches(transactions):
                    result = import_batch(
                        destination_url,
                        destination_token,
                        batch,
                        session=session,
                    )
                    total_batches += 1
                    total_imported += int(result.get("imported", len(batch)))
                    log.info(
                        "Lot %s importé: %s transaction(s)",
                        total_batches,
                        len(batch),
                    )
            except As24SyncError as exc:
                raise As24SyncError(
                    f"{exc} — relancer avec --from {day.isoformat()}"
                    f" --to {days[-1].isoformat()} pour reprendre"
                ) from exc

    if args.dry_run:
        log.info("Dry-run terminé: %s transaction(s) validée(s)", total_transactions)
    elif total_transactions == 0:
        log.info("Aucune transaction à importer sur la période")
    else:
        log.info(
            "Synchronisation terminée: %s transaction(s), %s lot(s)",
            total_imported,
            total_batches,
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    try:
        return run(parse_args(argv))
    except As24SyncError as exc:
        log.error("Synchronisation impossible: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
