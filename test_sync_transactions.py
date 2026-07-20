import json
import unittest
from datetime import date, datetime, timezone

from sync_transactions import (
    As24SyncError,
    build_as24_filter,
    extract_transaction_list,
    normalize_all_transactions,
    normalize_as24_transaction,
    paris_day_bounds_ms,
    parse_target_date,
    parse_target_days,
    transaction_batches,
)


class DateFilterTests(unittest.TestCase):
    def test_default_is_today_in_paris(self):
        now = datetime(2026, 7, 15, 22, 30, tzinfo=timezone.utc)
        self.assertEqual(parse_target_date(None, now), date(2026, 7, 16))

    def test_bounds_follow_dst(self):
        summer_start, summer_end = paris_day_bounds_ms(date(2026, 7, 15))
        winter_start, winter_end = paris_day_bounds_ms(date(2026, 1, 15))
        self.assertEqual(summer_start, 1784066400000)
        self.assertEqual(summer_end - summer_start + 1, 86_400_000)
        self.assertEqual(winter_start, 1768431600000)
        self.assertEqual(winter_end - winter_start + 1, 86_400_000)

    def test_filter_requests_every_transaction_type(self):
        payload = build_as24_filter(date(2026, 7, 15))
        self.assertEqual(payload["supportOffers"], [])
        self.assertEqual(payload["products"], [])
        self.assertNotIn("state", payload)


class TargetDaysTests(unittest.TestCase):
    NOW = datetime(2026, 7, 15, 22, 30, tzinfo=timezone.utc)

    def test_default_replays_j_minus_seven_through_today(self):
        self.assertEqual(
            parse_target_days(None, None, None, self.NOW),
            [
                date(2026, 7, 9),
                date(2026, 7, 10),
                date(2026, 7, 11),
                date(2026, 7, 12),
                date(2026, 7, 13),
                date(2026, 7, 14),
                date(2026, 7, 15),
                date(2026, 7, 16),
            ],
        )

    def test_explicit_date_keeps_single_day_behavior(self):
        self.assertEqual(
            parse_target_days("2026-07-14", None, None, self.NOW),
            [date(2026, 7, 14)],
        )

    def test_range_is_inclusive_and_ordered(self):
        days = parse_target_days(None, "2026-07-12", "2026-07-14", self.NOW)
        self.assertEqual(
            days,
            [date(2026, 7, 12), date(2026, 7, 13), date(2026, 7, 14)],
        )

    def test_range_defaults_to_today(self):
        days = parse_target_days(None, "2026-07-13", None, self.NOW)
        self.assertEqual(days[0], date(2026, 7, 13))
        self.assertEqual(days[-1], date(2026, 7, 16))

    def test_rejects_invalid_combinations(self):
        with self.assertRaisesRegex(As24SyncError, "--to nécessite --from"):
            parse_target_days(None, None, "2026-07-14", self.NOW)
        with self.assertRaisesRegex(As24SyncError, "mutuellement exclusifs"):
            parse_target_days("2026-07-14", "2026-07-12", None, self.NOW)
        with self.assertRaisesRegex(As24SyncError, "--from doit précéder --to"):
            parse_target_days(None, "2026-07-14", "2026-07-12", self.NOW)
        with self.assertRaisesRegex(As24SyncError, "format YYYY-MM-DD"):
            parse_target_days(None, "14/07/2026", None, self.NOW)


class NormalizeTests(unittest.TestCase):
    def setUp(self):
        self.raw = {
            "transactionId": 987654,
            "transactionNumber": "T-42",
            "transactionDate": 1784104200000,
            "supportId": "1234",
            "supportExtension": "02",
            "supportOffer": 11,
            "vehicle": {"immatriculation": "AB-123-CD"},
            "stationName": "AS24 Lille",
            "stationCountry": "FRA",
            "productLabel": "Gazole",
            "quantity": "82,45",
            "unit": "LT",
            "localCurrency": "EUR",
            "localExcludingTaxAmount": "124,47",
            "localVatAmount": 24.89,
            "localIncludingTaxAmount": 149.36,
        }

    def test_maps_projection_and_keeps_exact_raw_payload(self):
        result = normalize_as24_transaction(self.raw)
        self.assertEqual(result["external_id"], "987654")
        self.assertEqual(result["transaction_at"], "2026-07-15T08:30:00Z")
        self.assertEqual(result["card_reference"], "1234-02")
        self.assertEqual(result["vehicle_registration"], "AB-123-CD")
        self.assertEqual(result["quantity"], 82.45)
        self.assertEqual(result["unit"], "LT")
        self.assertEqual(result["country_code"], "FRA")
        self.assertIs(result["raw_payload"], self.raw)

    def test_keeps_non_fuel_transaction_without_quantity(self):
        toll = {
            "transactionId": "TOLL-1",
            "transactionDate": "2026-07-15T10:00:00+02:00",
            "supportOffer": 500,
            "productLabel": "Péage",
            "stationName": None,
        }
        result = normalize_as24_transaction(toll)
        self.assertEqual(result["support_offer"], 500)
        self.assertIsNone(result["quantity"])
        self.assertIsNone(result["station_name"])
        self.assertEqual(result["raw_payload"], toll)

    def test_keeps_negative_credit_values(self):
        credit = {
            **self.raw,
            "transactionId": "CREDIT-1",
            "quantity": -82.45,
            "localExcludingTaxAmount": -124.47,
            "localVatAmount": -24.89,
            "localIncludingTaxAmount": -149.36,
        }
        result = normalize_as24_transaction(credit)
        self.assertEqual(result["quantity"], -82.45)
        self.assertEqual(result["amount_ht"], -124.47)

    def test_rejects_row_without_stable_id(self):
        with self.assertRaisesRegex(As24SyncError, "identifiant stable"):
            normalize_as24_transaction({"transactionDate": 1784104200000})

    def test_falls_back_to_exit_date_when_transaction_date_invalid(self):
        toll = {
            "transactionId": "TOLL-2",
            "transactionDate": 0,
            "exitTransactionDate": 1784104200000,
            "productLabel": "Télépéage",
        }
        result = normalize_as24_transaction(toll)
        self.assertEqual(result["transaction_at"], "2026-07-15T08:30:00Z")

    def test_falls_back_to_invoice_date_for_service_rows(self):
        service = {
            "transactionId": "SV8290PFA1960218",
            "transactionDate": None,
            "exitTransactionDate": None,
            "invoiceDate": 1784104200000,
            "productLabel": "Frais de service",
        }
        result = normalize_as24_transaction(service)
        self.assertEqual(result["transaction_at"], "2026-07-15T08:30:00Z")

    def test_reports_all_values_when_no_date_is_usable(self):
        toll = {
            "transactionId": "TOLL-3",
            "transactionDate": 0,
            "exitTransactionDate": None,
        }
        with self.assertRaisesRegex(
            As24SyncError,
            r"transactionDate=0, exitTransactionDate=None, invoiceDate=None",
        ):
            normalize_as24_transaction(toll)

    def test_skips_dateless_rows_and_keeps_the_rest(self):
        dateless = {"transactionId": "BAD-1", "transactionDate": 0}
        with self.assertLogs("as24.transactions", level="WARNING") as captured:
            result = normalize_all_transactions([dateless, self.raw])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["external_id"], "987654")
        self.assertIn("BAD-1", captured.output[0])

    def test_deduplicates_only_identical_as24_rows(self):
        rows = [self.raw, dict(self.raw)]
        result = normalize_all_transactions(rows)
        self.assertEqual(len(result), 1)

    def test_rejects_same_id_with_two_different_payloads(self):
        rows = [self.raw, {**self.raw, "productLabel": "Gazole B7"}]
        with self.assertRaisesRegex(As24SyncError, "deux payloads différents"):
            normalize_all_transactions(rows)


class ResponseAndBatchTests(unittest.TestCase):
    def test_accepts_direct_and_wrapped_lists(self):
        rows = [{"transactionId": 1}]
        self.assertEqual(extract_transaction_list(rows), rows)
        self.assertEqual(extract_transaction_list({"transactions": rows}), rows)

    def test_batches_respect_item_and_byte_limits(self):
        rows = [
            {
                "external_id": str(index),
                "raw_payload": {"payload": "x" * 80},
            }
            for index in range(5)
        ]
        batches = list(transaction_batches(rows, max_items=2, max_bytes=500))
        self.assertEqual([len(batch) for batch in batches], [2, 2, 1])
        for batch in batches:
            size = len(
                json.dumps(
                    {"transactions": batch},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            self.assertLess(size, 500)


if __name__ == "__main__":
    unittest.main()
