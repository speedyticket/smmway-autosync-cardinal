"""Offline checks: python -m unittest discover -s tests -v.

The supplied workspace has no Cardinal runtime or pytest. These fakes implement
only contracts found in the three supplied plugins; no test can make HTTP calls.
"""
import copy
import importlib.util
import json
import logging
import sys
import tempfile
import threading
import types
import unittest
from dataclasses import replace
from decimal import Decimal as D, localcontext
from pathlib import Path
from unittest.mock import Mock, patch


class RequestException(Exception):
    pass


class Timeout(RequestException):
    pass


class ConnectionError(RequestException):
    pass


class RequestFailedError(Exception):
    def __init__(self, status=None, response=None):
        self.status_code, self.response = status, response


class Button:
    def __init__(self, text, callback_data):
        self.text, self.callback_data = text, callback_data


class Keyboard:
    def __init__(self):
        self.keyboard = []

    def row(self, *buttons):
        self.keyboard.append(buttons)
        return self


requests = types.ModuleType("requests")
requests.RequestException, requests.Timeout, requests.ConnectionError = RequestException, Timeout, ConnectionError
requests.Session = Mock(side_effect=AssertionError("Unexpected network session"))
telebot = types.ModuleType("telebot")
telebot_types = types.ModuleType("telebot.types")
telebot_types.InlineKeyboardButton, telebot_types.InlineKeyboardMarkup = Button, Keyboard
fp = types.ModuleType("FunPayAPI")
common = types.ModuleType("FunPayAPI.common")
common.exceptions = types.SimpleNamespace(RequestFailedError=RequestFailedError)
dependencies = {"requests": requests, "telebot": telebot, "telebot.types": telebot_types,
                "FunPayAPI": fp, "FunPayAPI.common": common}
spec = importlib.util.spec_from_file_location("autosync_under_test", Path(__file__).resolve().parents[1] / "autosync.py")
sync = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = sync
with patch.dict(sys.modules, dependencies):
    spec.loader.exec_module(sync)
sync.logger.addHandler(logging.NullHandler())
sync.logger.propagate = False  # Expected failure cases stay quiet; assertLogs still captures evidence.


DESCRIPTION = "smm: on\nid: 123\nname: way\nam: 1000"
SERVICE = sync.SmmService(123, "Telegram subscribers", "Telegram", D("6"), 1, 100000)
CFG = sync.PluginConfig(api_key="TEST_SECRET_KEY_1234")


class Fields:
    def __init__(self, lot_id=1, description=DESCRIPTION, price="10", english="", title="Test lot"):
        self.lot_id, self.description_ru, self.description_en = lot_id, description, english
        self.price, self.title_ru, self.title_en, self.csrf_token = price, title, "", "stale"
        self.fields = {"offer_id": str(lot_id), "active": "on", "amount": "999", "secrets": "keep me",
                       "auto_delivery": "on", "title_ru": title, "unrelated": "preserve me"}
        self.renew_fields()

    def renew_fields(self):
        self.fields.update(price=str(self.price), description_ru=self.description_ru,
                           description_en=self.description_en, csrf_token=self.csrf_token)


class Account:
    def __init__(self, lots):
        self.lots = {lot.lot_id: copy.deepcopy(lot) for lot in lots}
        self.csrf_token = "current-csrf"
        self.saved = []
        self.get_lot_fields = Mock(side_effect=lambda lot_id: copy.deepcopy(self.lots[lot_id]))
        self.save_lot = Mock(side_effect=self._save)

    def _save(self, fields):
        self.saved.append(copy.deepcopy(fields))
        self.lots[fields.lot_id] = copy.deepcopy(fields)


class Telegram:
    def __init__(self):
        self.authorized_users = {7: "admin"}
        self.bot = Mock()
        self.next_message_id = 1000
        self.bot.send_message.side_effect = self._send
        self.states = {}
        self.msg_handler, self.cbq_handler = Mock(), Mock()

    def _send(self, *args, **kwargs):
        message = types.SimpleNamespace(message_id=self.next_message_id)
        self.next_message_id += 1
        return message

    def set_state(self, chat, mid, user, state, data):
        self.states[chat, user] = {"state": state, "mid": mid}

    def check_state(self, chat, user, state):
        return self.states.get((chat, user), {}).get("state") == state

    def get_state(self, chat, user):
        return self.states.get((chat, user))

    def clear_state(self, chat, user):
        self.states.pop((chat, user), None)


def cardinal(lots=()):
    return types.SimpleNamespace(account=Account(lots), update_lots_and_categories=Mock(),
                                 tg_profile=types.SimpleNamespace(get_common_lots=lambda: [types.SimpleNamespace(id=lot.lot_id) for lot in lots]),
                                 telegram=Telegram(), add_telegram_commands=Mock())


class MemoryStore:
    def __init__(self, cfg=CFG):
        self.cfg = cfg

    def snapshot(self):
        return self.cfg

    def update(self, **changes):
        self.cfg = replace(self.cfg, **changes)


class OfflineCase(unittest.TestCase):
    def setUp(self):
        self.sleep = patch.object(sync.time, "sleep").start()
        self.addCleanup(patch.stopall)

    def engine(self, lots, services=None, error=None):
        c = cardinal(lots)
        client = Mock()
        client.get_services = Mock(side_effect=error, return_value={123: SERVICE} if services is None else services)
        engine = sync.SyncEngine(c, Mock(return_value=client))
        return engine, c, client


class ParserTests(OfflineCase):
    def test_marked_standard_lot(self):
        result = sync.parse_lot(42, DESCRIPTION, "10")
        self.assertEqual(result.status, "matched")
        self.assertEqual((result.lot.service_id, result.lot.amount, result.lot.current_price), (123, 1000, D("10")))

    def test_default_amount_and_unmarked_scope(self):
        text = "id:123\nname:way"
        self.assertEqual(sync.parse_lot(1, text, "1").status, "marker")
        parsed = sync.parse_lot(1, text, "1", "all_smmway")
        self.assertEqual(parsed.status, "matched")
        self.assertEqual(parsed.lot.amount, 1)

    def test_all_key_aliases_case_whitespace_and_equals(self):
        for sid in ("id", "service_id", "service"):
            for amount in ("am", "nam", "quantity", "qty", "количество"):
                for provider in ("name", "service_name", "provider"):
                    with self.subTest(sid=sid, amount=amount, provider=provider):
                        text = f" SMM = ON \r\n {sid.upper()} = 123 \r\n {amount.upper()} : 7\n {provider} : SMMWay"
                        result = sync.parse_lot(1, text, "1")
                        self.assertEqual(result.status, "matched")
                        self.assertEqual(result.lot.amount, 7)

    def test_absent_provider_uses_autosmm_default(self):
        self.assertEqual(sync.parse_lot(1, "smm:on\nid:123", "1").status, "matched")

    def test_other_and_unknown_providers_excluded(self):
        for provider in ("never", "NeverSMM", "unknown", ""):
            with self.subTest(provider=provider):
                self.assertEqual(sync.parse_lot(1, f"smm:on\nid:123\nname:{provider}", "1").status, "provider")

    def test_off_always_wins(self):
        for scope in ("marked_only", "all_smmway"):
            self.assertEqual(sync.parse_lot(1, DESCRIPTION + "\nsmm:off", "1", scope).status, "off")

    def test_invalid_id_and_amount(self):
        for key in ("id", "am"):
            for value in ("0", "-1", "abc", "12x", "1.5", "", "123 extra"):
                text = f"smm:on\nname:way\n{key}:{value}" + ("\nid:123" if key == "am" else "")
                with self.subTest(key=key, value=value):
                    self.assertEqual(sync.parse_lot(1, text, "1").status, "malformed")

    def test_conflicting_ids_and_amounts_rejected(self):
        for extra in ("id:124", "service_id:124", "quantity:2"):
            self.assertEqual(sync.parse_lot(1, DESCRIPTION + "\n" + extra, "1").status, "malformed")

    def test_same_duplicate_id_is_unambiguous(self):
        self.assertEqual(sync.parse_lot(1, DESCRIPTION + "\nservice_id:00123", "1").status, "matched")

    def test_no_substring_id_search(self):
        self.assertEqual(sync.parse_lot(1, "See id: 123 and name:way in our text", "1", "all_smmway").status, "unrelated")

    def test_english_fallback_and_source(self):
        parsed = sync.parse_fields(1, Fields(description="Описание товара", english=DESCRIPTION), "marked_only")
        self.assertEqual(parsed.lot.source, "description_en")

    def test_english_cannot_bypass_unsafe_russian_block(self):
        for text, expected in (("smm:off", "off"), (DESCRIPTION.replace("way", "never"), "provider"),
                               (DESCRIPTION + "\nid:124", "malformed")):
            self.assertEqual(sync.parse_fields(1, Fields(description=text, english=DESCRIPTION), "marked_only").status, expected)

    def test_conflicting_ru_en_rejected(self):
        self.assertEqual(sync.parse_fields(1, Fields(english=DESCRIPTION.replace("123", "124")), "marked_only").status, "malformed")

    def test_marker_in_english_with_equivalent_russian_metadata(self):
        lot = Fields(description=DESCRIPTION.replace("smm: on\n", ""), english=DESCRIPTION)
        result = sync.parse_fields(1, lot, "marked_only")
        self.assertEqual(result.status, "matched")
        self.assertEqual(result.lot.source, "description_en")


class PricingTests(OfflineCase):
    def test_decimal_formula_and_round_up(self):
        self.assertEqual(sync.calculate_new_price("6.1234567", 1000, CFG), D("9.186"))
        self.assertEqual(sync.calculate_new_price("1", 333, replace(CFG, multiplier=D("1"))), D("0.333"))

    def test_both_lowers_and_up_only_prevents_lowering(self):
        lot = sync.ParsedLot(1, 123, 1000, D("10"))
        self.assertEqual(sync.evaluate_price(lot, SERVICE, CFG).status, "updated")
        self.assertEqual(sync.evaluate_price(lot, SERVICE, replace(CFG, direction="up_only")).status, "up_only")

    def test_threshold_boundary_and_zero(self):
        for old, threshold, status in (("8.90", "0.1", "updated"), ("8.95", "0.1", "threshold"),
                                       ("9.10", "0.1", "updated"), ("9", "0", "unchanged"), ("8.9999", "0", "updated")):
            result = sync.evaluate_price(sync.ParsedLot(1, 123, 1000, D(old)), SERVICE, replace(CFG, threshold=D(threshold)))
            self.assertEqual(result.status, status)

    def test_low_positive_price_is_accepted(self):
        result = sync.evaluate_price(sync.ParsedLot(1, 123, 1, D("1")), SERVICE, CFG)
        self.assertEqual(result.status, "updated")
        self.assertEqual(result.new_price, D("0.009"))

    def test_funpay_float_precision_guard_matches_preview_and_save(self):
        service = replace(SERVICE, rate=D("1000000000000000.0013"))
        result = sync.evaluate_price(sync.ParsedLot(1, 123, 1000, D("10")), service,
                                     replace(CFG, multiplier=D("1")))
        self.assertEqual(result.status, "invalid_price")
        self.assertIn("точность", result.reason)

    def test_calculation_is_independent_of_ambient_decimal_context(self):
        lot = sync.ParsedLot(1, 123, 1000, D("9.1849888"))
        service = replace(SERVICE, rate=D("6.1234567"))
        with localcontext() as context:
            context.prec = 3
            result = sync.evaluate_price(lot, service, replace(CFG, threshold=D("0.0001111")))
        self.assertEqual(result.new_price, D("9.186"))
        self.assertEqual(result.status, "updated")

    def test_invalid_rates(self):
        for rate in ("0", "-1", "NaN", "Infinity"):
            with self.assertRaises(ValueError):
                sync.calculate_new_price(rate, 1000, CFG)

    def test_funpay_normalization_examples_without_intermediate_rounding(self):
        for raw, normalized in (("0.0541", "0.055"), ("0.0098", "0.010"), ("0.0042", "0.005"),
                                ("0.0003", "0.001"), ("0.0017", "0.002"), ("0.05400001", "0.055"),
                                ("0.055", "0.055")):
            with self.subTest(raw=raw):
                price = sync.calculate_new_price(D(raw) * 1000, 1, replace(CFG, multiplier=D(1)))
                self.assertEqual(price, D(normalized))
                self.assertEqual(price.as_tuple().exponent, -3)

    def test_threshold_and_direction_use_normalized_funpay_price(self):
        cfg = replace(CFG, multiplier=D(1))
        service = replace(SERVICE, rate=D("54.1"))
        for old, threshold, direction, status in (("0.054", "0.001", "both", "updated"),
                                                 ("0.054", "0.002", "both", "threshold"),
                                                 ("0.055", "0", "both", "unchanged"),
                                                 ("0.056", "0", "up_only", "up_only")):
            with self.subTest(old=old, threshold=threshold, direction=direction):
                lot = sync.ParsedLot(1, 123, 1, D(old))
                result = sync.evaluate_price(lot, service, replace(cfg, threshold=D(threshold), direction=direction))
                self.assertEqual(result.new_price, D("0.055"))
                self.assertEqual(result.status, status)


class EngineTests(OfflineCase):
    def test_normalized_preview_sync_report_and_repeat_use_same_price(self):
        for rate, expected in (("54.1", "0.055"), ("9.8", "0.010"), ("4.2", "0.005"), ("0.3", "0.001")):
            with self.subTest(rate=rate):
                cfg = replace(CFG, multiplier=D(1), threshold=D(0))
                original = Fields(description=DESCRIPTION.replace("am: 1000", "am: 1"), price="1")
                engine, c, _ = self.engine([original], {123: replace(SERVICE, rate=D(rate))})
                preview = engine.run(cfg, "preview")
                self.assertEqual(preview.results[0].new_price, D(expected))
                self.assertIn(expected, sync.format_report(preview))
                c.account.save_lot.assert_not_called()
                actual = engine.run(cfg)
                self.assertEqual(actual.results[0].new_price, D(expected))
                self.assertEqual(D(str(c.account.saved[0].price)), D(expected))
                self.assertIn(expected, sync.format_report(actual))
                repeat = engine.run(cfg)
                self.assertEqual(repeat.counts["unchanged"], 1)
                self.assertEqual(c.account.save_lot.call_count, 1)
                self.assertEqual(c.account.lots[1].description_ru, original.description_ru)

    def test_sync_changes_only_price_and_csrf(self):
        original = Fields(description=DESCRIPTION + "\nNumber 123 occurs elsewhere.")
        engine, c, client = self.engine([original])
        report = engine.run(CFG)
        self.assertEqual(report.counts["updated"], 1)
        self.assertEqual(report.counts["decreased"], 1)
        saved = c.account.saved[0]
        self.assertEqual(saved.price, 9.0)
        self.assertEqual(saved.csrf_token, c.account.csrf_token)
        self.assertEqual({k: v for k, v in saved.fields.items() if k not in {"price", "csrf_token"}},
                         {k: v for k, v in original.fields.items() if k not in {"price", "csrf_token"}})
        self.assertEqual(c.account.get_lot_fields.call_count, 2)
        client.get_services.assert_called_once()
        client.close.assert_called_once()

    def test_missing_service_never_saves_or_replaces(self):
        original = Fields()
        candidate = replace(SERVICE, service_id=456)
        engine, c, _ = self.engine([original], {456: candidate})
        report = engine.run(CFG)
        self.assertEqual(report.counts["dead"], 1)
        c.account.save_lot.assert_not_called()
        self.assertEqual(c.account.lots[1].description_ru, DESCRIPTION)

    def test_preview_matches_sync_without_save(self):
        lots = [Fields(1, price="10"), Fields(2, price="8"), Fields(3, price="9"), Fields(4, price="8.95"),
                Fields(5, description=DESCRIPTION.replace("123", "456"))]
        engine, c, client = self.engine(lots)
        report = engine.run(CFG, "preview")
        c.account.save_lot.assert_not_called()
        self.assertEqual({key: report.counts[key] for key in ("matched", "updated", "increased", "decreased", "unchanged", "threshold", "dead")},
                         dict(matched=5, updated=2, increased=1, decreased=1, unchanged=1, threshold=1, dead=1))
        client.get_services.assert_called_once()
        actual, _, _ = self.engine(lots)
        self.assertEqual(report.counts, actual.run(CFG).counts)

    def test_dead_diagnostics_are_read_only(self):
        engine, c, _ = self.engine([Fields(1), Fields(2, description=DESCRIPTION.replace("123", "999"))])
        report = engine.run(CFG, "dead")
        c.account.save_lot.assert_not_called()
        page = sync.dead_id_pages(report)[0]
        for value in ("#2", "999", "1000", "10", "Test lot"):
            self.assertIn(value, page)

    def test_failed_catalog_aborts_before_any_funpay_call(self):
        engine, c, _ = self.engine([Fields()], error=sync.SmmWayAPIError("unavailable"))
        report = engine.run(CFG)
        c.update_lots_and_categories.assert_not_called()
        c.account.get_lot_fields.assert_not_called()
        c.account.save_lot.assert_not_called()
        text = sync.format_report(report)
        self.assertIn("Цены лотов не изменялись", text)
        self.assertNotIn("Обновлено", text)

    def test_client_construction_failure_aborts_as_catalog_error(self):
        engine, c, _ = self.engine([Fields()])
        engine.client_factory.side_effect = RuntimeError("Session creation failed")
        report = engine.run(CFG)
        self.assertIn("каталог", report.fatal)
        c.account.get_lot_fields.assert_not_called()

    def test_close_failure_does_not_erase_successful_report(self):
        engine, _, client = self.engine([Fields()])
        client.close.side_effect = RuntimeError("close failed")
        self.assertEqual(engine.run(CFG).counts["updated"], 1)

    def test_changed_service_amount_provider_or_off_prevents_save(self):
        for description in (DESCRIPTION.replace("123", "124"), DESCRIPTION.replace("1000", "100"),
                            DESCRIPTION.replace("way", "never"), DESCRIPTION + "\nsmm:off"):
            engine, c, _ = self.engine([Fields()])
            c.account.get_lot_fields.side_effect = [Fields(), Fields(description=description)]
            report = engine.run(CFG)
            self.assertEqual(report.counts["changed"], 1)
            c.account.save_lot.assert_not_called()

    def test_latest_price_rechecks_threshold(self):
        engine, c, _ = self.engine([Fields()])
        c.account.get_lot_fields.side_effect = [Fields(), Fields(price="8.95")]
        self.assertEqual(engine.run(CFG).counts["threshold"], 1)
        c.account.save_lot.assert_not_called()

    def test_latest_description_is_preserved(self):
        engine, c, _ = self.engine([Fields()])
        changed = Fields(description=DESCRIPTION + "\nNew buyer instructions")
        c.account.get_lot_fields.side_effect = [Fields(), changed]
        engine.run(CFG)
        self.assertEqual(c.account.saved[0].description_ru, changed.description_ru)

    def test_save_retry_refetches_and_stops_if_metadata_changes(self):
        engine, c, _ = self.engine([Fields()])
        c.account.get_lot_fields.side_effect = [Fields(), Fields(), Fields(description=DESCRIPTION.replace("123", "456"))]
        c.account.save_lot.side_effect = RequestFailedError(429)
        report = engine.run(CFG)
        self.assertEqual(c.account.save_lot.call_count, 1)
        self.assertEqual(report.counts["changed"], 1)

    def test_save_failure_does_not_prevent_next_lot(self):
        engine, c, _ = self.engine([Fields(1), Fields(2)])
        c.account.save_lot.side_effect = [RuntimeError("save failed"), None]
        report = engine.run(CFG)
        self.assertEqual(report.counts["save_error"], 1)
        self.assertEqual(report.counts["updated"], 1)

    def test_progress_errors_do_not_stop_scan_and_updates_are_throttled(self):
        engine, _, _ = self.engine([Fields(i) for i in range(1, 12)])
        progress = Mock(side_effect=RuntimeError("Telegram unavailable"))
        report = engine.run(CFG, "preview", progress)
        self.assertEqual(report.counts["scanned"], 11)
        self.assertEqual(progress.call_count, 4)  # 1, 5, 10, last.

    def test_progress_exception_secrets_are_redacted_in_debug_logs(self):
        engine, _, _ = self.engine([Fields()])
        with self.assertLogs(sync.logger, level="DEBUG") as logs:
            engine.run(CFG, "preview", Mock(side_effect=RuntimeError(CFG.api_key)))
        self.assertNotIn(CFG.api_key, " ".join(logs.output))

    def test_scope_and_provider_report(self):
        engine, c, _ = self.engine([Fields(1, description=DESCRIPTION.replace("way", "never")),
                                    Fields(2, description=DESCRIPTION.replace("smm: on\n", "")),
                                    Fields(3, description=DESCRIPTION + "\nsmm:off"),
                                    Fields(4, description=DESCRIPTION + "\nservice:456")])
        report = engine.run(CFG)
        for status in ("provider", "marker", "off", "malformed"):
            self.assertEqual(report.counts[status], 1)
        c.account.save_lot.assert_not_called()


def response(status=200, data=None, headers=None):
    return types.SimpleNamespace(status_code=status, headers=headers or {},
                                 json=Mock(return_value=[{"service": "123", "rate": "6", "name": "TG", "category": "Telegram", "min": "1", "max": "9999"}] if data is None else data))


class RetryAndApiTests(OfflineCase):
    def request(self, outcomes):
        session = Mock()
        session.post.side_effect = outcomes
        with patch.object(sync.requests, "Session", return_value=session):
            client = sync.SmmWayClient(CFG.api_key)
        return client, session

    def test_post_body_timeout_session_and_catalog_model(self):
        client, session = self.request([response()])
        result = client.get_services()
        self.assertEqual(result[123].rate, D("6"))
        self.assertEqual(result[123].minimum, 1)
        args, kwargs = session.post.call_args
        self.assertNotIn(CFG.api_key, args[0])
        self.assertEqual(kwargs["data"], {"action": "services", "key": CFG.api_key})
        self.assertNotIn("params", kwargs)
        self.assertEqual(kwargs["timeout"], (5, 20))
        self.assertFalse(kwargs["allow_redirects"])
        client.close()
        session.close.assert_called_once()

    def test_retry_429_5xx_and_network_errors(self):
        client, session = self.request([response(429, headers={"Retry-After": "4"}), Timeout(CFG.api_key),
                                        response(502), ConnectionError(CFG.api_key), response()])
        with self.assertLogs(sync.logger, level="WARNING") as logs:
            self.assertIn(123, client.get_services())
        self.assertNotIn(CFG.api_key, " ".join(logs.output))
        self.assertEqual(session.post.call_count, 5)
        self.assertGreaterEqual(self.sleep.call_args_list[0].args[0], 4)

    def test_retries_exhaust_and_ordinary_4xx_not_retried(self):
        for status, count in ((429, sync.SMM_ATTEMPTS), (503, sync.SMM_ATTEMPTS), (401, 1), (403, 1), (400, 1)):
            client, session = self.request([response(status)] * sync.SMM_ATTEMPTS)
            with self.assertRaises(sync.SmmWayAPIError):
                client.get_services()
            self.assertEqual(session.post.call_count, count)

    def test_invalid_catalog_is_error_not_empty_success(self):
        for data in ([], {}, {"error": "user_inactive"}, [{"service": 123, "rate": "NaN"}],
                     [{"service": 123, "rate": "0"}], [{"service": 123}],
                     [{"service": 123, "rate": "1"}, {"service": 123, "rate": "2"}]):
            client, _ = self.request([response(data=data)])
            with self.subTest(data=data), self.assertRaises(sync.SmmWayAPIError):
                client.get_services()

    def test_bad_catalog_entry_preserves_other_valid_services(self):
        valid = {"service": 123, "rate": "6", "name": "TG", "category": "Telegram"}
        for bad in (None, [], {"rate": "6"}, {"service": "invalid", "rate": "6"},
                    {"service": 456}, {"service": 456, "rate": "NaN"},
                    {"service": 456, "rate": "0"}, {"service": 456, "rate": "-1"},
                    {"service": 456, "rate": "6", "min": "broken"},
                    {"service": 456, "rate": "6", "max": {"bad": "field"}}):
            with self.subTest(bad=bad):
                client, session = self.request([response(data=[bad, valid])])
                with self.assertLogs(sync.logger, level="WARNING"):
                    catalog = client.get_services()
                self.assertEqual(set(catalog), {123})
                self.assertEqual(catalog[123].rate, D("6"))
                session.post.assert_called_once()

    def test_duplicate_conflicts_and_broken_ids_cannot_be_revived(self):
        valid = {"service": 123, "rate": "6"}
        other = {"service": 456, "rate": "8"}
        for bad in ({"service": 123, "rate": "7"}, {"service": 123, "rate": "NaN"},
                    {"service": 123}, {"service": 123, "rate": "6", "min": "broken"}):
            for entries in ([valid, bad, valid, other], [bad, valid, other], [other, valid, bad]):
                with self.subTest(bad=bad, entries=entries):
                    client, _ = self.request([response(data=entries)])
                    self.assertEqual(set(client.get_services()), {456})

    def test_identical_duplicates_remain_usable(self):
        entry = {"service": 123, "rate": "6"}
        client, _ = self.request([response(data=[entry, dict(entry)])])
        self.assertEqual(set(client.get_services()), {123})

    def test_invalid_optional_entry_does_not_expose_secret_in_warning(self):
        client, _ = self.request([response(data=[{"service": 123, "rate": "6"},
                                                {"service": 456, "rate": "8", "min": CFG.api_key}])])
        with self.assertLogs(sync.logger, level="WARNING") as logs:
            self.assertEqual(set(client.get_services()), {123})
        self.assertNotIn(CFG.api_key, " ".join(logs.output))

    def test_unusable_service_lot_is_dead_and_other_lot_still_syncs(self):
        client, _ = self.request([response(data=[{"service": 123, "rate": "6"},
                                                {"service": 123, "rate": "7"},
                                                {"service": 456, "rate": "6"}])])
        c = cardinal([Fields(1), Fields(2, description=DESCRIPTION.replace("123", "456"))])
        engine = sync.SyncEngine(c, Mock(return_value=client))
        report = engine.run(CFG)
        self.assertEqual(report.counts["dead"], 1)
        self.assertEqual(report.counts["updated"], 1)
        self.assertEqual([lot.lot_id for lot in c.account.saved], [2])
        self.assertEqual(c.account.lots[1].description_ru, DESCRIPTION)
        self.assertEqual(c.account.lots[1].price, "10")
        self.assertIn("123", report.results[0].reason)

    def test_fully_invalid_catalog_aborts_before_funpay_access(self):
        for entries in ([None, {"service": "invalid", "rate": "6"}],
                        [{"service": 123, "rate": "NaN"}],
                        [{"service": 123, "rate": "6"}, {"service": 123, "rate": "7"}]):
            with self.subTest(entries=entries):
                client, _ = self.request([response(data=entries)])
                c = cardinal([Fields()])
                report = sync.SyncEngine(c, Mock(return_value=client)).run(CFG)
                self.assertTrue(report.fatal)
                c.update_lots_and_categories.assert_not_called()
                c.account.get_lot_fields.assert_not_called()
                c.account.save_lot.assert_not_called()
                self.assertNotIn("Обновлено", sync.format_report(report))

    def test_api_error_and_traceback_redact_secret(self):
        client, _ = self.request([response(data={"error": "invalid " + CFG.api_key})])
        with self.assertRaises(sync.SmmWayAPIError) as error:
            client.get_services()
        self.assertNotIn(CFG.api_key, str(error.exception))
        engine, _, _ = self.engine([Fields()], error=RuntimeError("echo " + CFG.api_key))
        with self.assertLogs(sync.logger, level="ERROR") as logs:
            report = engine.run(CFG)
        self.assertNotIn(CFG.api_key, " ".join(logs.output))
        self.assertNotIn(CFG.api_key, sync.format_report(report))

    def test_funpay_status_from_exception_or_response(self):
        for error in (RequestFailedError(429), RequestFailedError(response=response(429)), Timeout("network")):
            function = Mock(side_effect=[error, "ok"])
            self.assertEqual(sync.funpay_retry(function), "ok")
            self.assertEqual(function.call_count, 2)

    def test_funpay_retry_is_bounded_and_4xx_not_retried(self):
        for status, expected in ((429, sync.FUNPAY_ATTEMPTS), (502, sync.FUNPAY_ATTEMPTS), (403, 1)):
            function = Mock(side_effect=RequestFailedError(status))
            with self.assertRaises(RequestFailedError):
                sync.funpay_retry(function)
            self.assertEqual(function.call_count, expected)


class ConfigTests(OfflineCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.new, self.old, self.shadow = (self.root / name for name in ("new/config.json", "old.json", "shadow.json"))

    def store(self):
        return sync.ConfigStore(self.new, self.old, self.shadow)

    def write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_migration_priority_and_no_legacy_deletion(self):
        self.write(self.old, {"enabled": True, "api_key": "old-key", "multiplier": 2.5, "threshold": 0})
        self.write(self.shadow, {"api_key": "shadow-key", "last_mult": 9, "auto_interval_sec": 1900, "notify_on_complete": False})
        store = self.store()
        cfg = store.snapshot()
        self.assertEqual((cfg.api_key, cfg.multiplier, cfg.update_interval, cfg.threshold, cfg.notify_on_complete),
                         ("old-key", D("2.5"), 1900, D(0), False))
        self.assertTrue(self.old.exists() and self.shadow.exists())
        self.assertTrue(self.new.exists())
        self.write(self.old, {"api_key": "different"})
        self.assertEqual(self.store().snapshot().api_key, "old-key")
        self.assertEqual(json.loads(self.new.read_text(encoding="utf-8"))["multiplier"], "2.5")

    def test_existing_new_config_wins(self):
        self.write(self.new, {"api_key": "new-key", "scope": "all_smmway"})
        self.write(self.old, {"enabled": True, "api_key": "old-key"})
        self.assertEqual(self.store().snapshot().api_key, "new-key")
        self.assertFalse(self.store().snapshot().enabled)

    def test_legacy_price_settings_load_but_cannot_override_funpay_format(self):
        self.write(self.new, {"min_price": "0.01", "price_decimals": 4, "rounding": "down", "multiplier": "1"})
        cfg = self.store().snapshot()
        self.assertEqual((cfg.min_price, cfg.price_decimals, cfg.rounding), (D("0.01"), 4, "down"))
        self.assertEqual(sync.calculate_new_price("0.3", 1, cfg), D("0.001"))
        self.assertEqual(sync.calculate_new_price("54.1", 1, cfg), D("0.055"))

    def test_corrupt_config_does_not_silently_restore_legacy_enabled(self):
        self.new.parent.mkdir(parents=True)
        self.new.write_text("{broken", encoding="utf-8")
        self.write(self.old, {"enabled": True, "api_key": "old-key"})
        self.assertEqual(self.store().snapshot(), sync.PluginConfig())
        self.assertEqual(self.new.read_text(encoding="utf-8"), "{broken")

    def test_defaults_are_independent_and_ranges_validated(self):
        cfg = sync.validate_config({"enabled": "true", "multiplier": "NaN", "threshold": -1, "update_interval": 1,
                                    "scope": "invalid", "price_decimals": 1000, "unknown": "ignored"})
        self.assertEqual(cfg, sync.PluginConfig())
        self.assertEqual(sync.validate_config({"multiplier": "2,5", "threshold": "0"}).threshold, D(0))
        self.store().update(multiplier="3,5")
        self.assertEqual(sync.DEFAULT_CONFIG["multiplier"], D("1.5"))

    def test_atomic_write_failure_keeps_old_config_and_memory(self):
        store = self.store()
        before = self.new.read_bytes()
        with patch.object(sync.os, "replace", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                store.update(multiplier="2")
        self.assertEqual(self.new.read_bytes(), before)
        self.assertEqual(store.snapshot().multiplier, D("1.5"))

    def test_changed_field_names_only_logged(self):
        store = self.store()
        with self.assertLogs(sync.logger, level="INFO") as logs:
            store.update(api_key=CFG.api_key)
        self.assertNotIn(CFG.api_key, " ".join(logs.output))
        self.assertNotIn(CFG.api_key, repr(store.snapshot()))


class RuntimeAndUiTests(OfflineCase):
    def runtime(self, cfg=CFG):
        c = cardinal([Fields()])
        runtime = sync.PluginRuntime(c, MemoryStore(cfg))
        runtime.engine = Mock()
        runtime.engine.run.return_value = sync.RunReport("sync")
        return runtime, c

    def message(self, text="", uid=7):
        return types.SimpleNamespace(chat=types.SimpleNamespace(id=100), message_id=22,
                                     from_user=types.SimpleNamespace(id=uid), text=text, content_type="text")

    def call(self, action, uid=7):
        return types.SimpleNamespace(id="callback", message=self.message(uid=uid),
                                     from_user=types.SimpleNamespace(id=uid), data=f"{sync.UUID}_{action}")

    def test_second_manual_auto_and_preview_run_share_lock(self):
        runtime, _ = self.runtime()
        entered, release = threading.Event(), threading.Event()

        def blocking(*args):
            entered.set()
            release.wait(3)
            return sync.RunReport("sync")

        runtime.engine.run.side_effect = blocking
        try:
            self.assertTrue(runtime.start_run(chat_id=100, message_id=22))
            self.assertTrue(entered.wait(1))
            worker = runtime.worker
            self.assertFalse(runtime.start_run())
            self.assertFalse(runtime.start_run("preview", 100, 22))
            self.assertFalse(runtime.start_run("dead", 100, 22))
            self.assertIs(runtime.worker, worker)
        finally:
            release.set()
            runtime.worker.join(3)
        self.assertFalse(sync.run_lock.locked())
        self.assertEqual(runtime.engine.run.call_count, 1)

    def test_lock_released_on_crash_and_thread_start_failure(self):
        runtime, _ = self.runtime()
        runtime.engine.run.side_effect = RuntimeError("worker failed")
        self.assertTrue(runtime.start_run())
        runtime.worker.join(3)
        self.assertFalse(sync.run_lock.locked())
        with patch.object(sync.threading, "Thread", side_effect=RuntimeError("thread creation failed")):
            with self.assertRaises(RuntimeError):
                runtime.start_run()
        self.assertFalse(sync.run_lock.locked())

    def test_automatic_launch_rechecks_enable_and_stop_after_lock(self):
        runtime, _ = self.runtime()
        self.assertFalse(runtime.start_run(automatic=True))
        self.assertFalse(sync.run_lock.locked())
        runtime.store.cfg = replace(CFG, enabled=True)
        original_snapshot = runtime.store.snapshot

        def stop_during_snapshot():
            runtime.stop()
            return original_snapshot()

        runtime.store.snapshot = stop_during_snapshot
        self.assertFalse(runtime.start_run())
        self.assertFalse(sync.run_lock.locked())

    def test_scheduler_one_thread_and_settings_wake(self):
        runtime, _ = self.runtime()
        with patch.object(sync.threading, "Thread") as thread:
            runtime.start_scheduler()
            runtime.start_scheduler()
            thread.assert_called_once()
            self.assertTrue(thread.call_args.kwargs["daemon"])
        runtime.update_config(update_interval=1900)
        self.assertTrue(runtime.wake.is_set())

    def test_scheduler_default_wait_change_interval_disable(self):
        runtime, _ = self.runtime(replace(CFG, enabled=True))
        runtime.start_run = Mock()
        event = Mock()
        timeouts = []

        def wait(timeout):
            timeouts.append(timeout)
            if len(timeouts) == 1:
                runtime.update_config(update_interval=1900)
            elif len(timeouts) == 2:
                runtime.update_config(enabled=False)
            else:
                runtime.stop()
            return True

        event.wait.side_effect = wait
        runtime.wake = event
        runtime._schedule()
        self.assertEqual(timeouts, [43200, 1900, None])
        runtime.start_run.assert_not_called()

    def test_scheduler_timeout_and_explicit_run_on_start(self):
        for startup in (False, True):
            runtime, _ = self.runtime(replace(CFG, enabled=True, run_on_start=startup))
            runtime.start_run = Mock()
            event = Mock()

            def wait(timeout):
                if event.wait.call_count == 2:
                    runtime.stop()
                    return True
                return False

            event.wait.side_effect = wait
            runtime.wake = event
            runtime._schedule()
            runtime.start_run.assert_called_once_with(automatic=True)
            self.assertEqual(event.wait.call_args_list[0].args[0], 0 if startup else 43200)

    def test_scheduler_stop_between_loop_condition_and_clear_is_not_lost(self):
        runtime, _ = self.runtime()
        runtime.start_run = Mock()
        event = Mock()
        event.clear.side_effect = runtime.stop
        runtime.wake = event
        runtime._schedule()
        event.wait.assert_not_called()
        runtime.start_run.assert_not_called()

    def test_scheduler_survives_launch_exception(self):
        runtime, _ = self.runtime(replace(CFG, enabled=True))
        runtime.start_run = Mock(side_effect=RuntimeError(CFG.api_key))
        event = Mock()

        def wait(timeout):
            if event.wait.call_count == 2:
                runtime.stop()
                return True
            return False

        event.wait.side_effect = wait
        runtime.wake = event
        with self.assertLogs(sync.logger, level="ERROR") as logs:
            runtime._schedule()
        self.assertEqual(event.wait.call_count, 2)
        self.assertNotIn(CFG.api_key, " ".join(logs.output))

    def test_manual_always_shown_auto_silent_unless_fatal(self):
        runtime, c = self.runtime(replace(CFG, notify_on_complete=False))
        for chat, fatal, expected in ((None, "", 0), (None, "catalog failed", 1), (100, "", 0)):
            c.telegram.bot.reset_mock()
            runtime.engine.run.return_value = sync.RunReport("sync", fatal=fatal)
            sync.run_lock.acquire()
            runtime._run(runtime.store.snapshot(), "sync", chat, 22)
            self.assertEqual(c.telegram.bot.send_message.call_count, expected)
            if chat:
                self.assertTrue(c.telegram.bot.edit_message_text.called)

    def test_every_problem_status_alerts_authorized_users_with_notifications_off(self):
        for status in ("dead", "malformed", "invalid_price", "read_error", "save_error", "changed", "fatal"):
            with self.subTest(status=status):
                runtime, c = self.runtime(replace(CFG, notify_on_complete=False))
                c.telegram.authorized_users[8] = "second admin"
                report = sync.RunReport("sync")
                if status == "fatal":
                    report.fatal = "Не удалось получить каталог SMMWay. Цены лотов не изменялись."
                else:
                    lot = sync.ParsedLot(42, 123, 1000, D("10"))
                    report.record(sync.LotResult(42, status, lot, reason="Причина <bad> & details"))
                runtime.engine.run.return_value = report
                sync.run_lock.acquire()
                runtime._run(runtime.store.snapshot(), "sync", None, None)
                self.assertEqual(c.telegram.bot.send_message.call_count, 2)
                self.assertEqual([call.args[0] for call in c.telegram.bot.send_message.call_args_list], [7, 8])
                text = c.telegram.bot.send_message.call_args.args[1]
                self.assertIn("обнаружены проблемы", text)
                if status == "fatal":
                    self.assertIn("Цены лотов не изменялись", text)
                    self.assertNotIn("обновлено:", text)
                else:
                    self.assertIn("#42:", text)
                    self.assertIn("Причина &lt;bad&gt; &amp; details", text)
                    if status == "dead":
                        self.assertIn("Service ID 123", text)

    def test_successful_auto_with_routine_skips_is_silent_when_notifications_off(self):
        runtime, c = self.runtime(replace(CFG, notify_on_complete=False))
        report = sync.RunReport("sync")
        for status in ("updated", "unchanged", "threshold", "up_only", "provider", "marker", "off", "unrelated"):
            report.record(sync.LotResult(42, status, sync.ParsedLot(42, 123, 1000, D("10")), D("9")))
        runtime.engine.run.return_value = report
        sync.run_lock.acquire()
        runtime._run(runtime.store.snapshot(), "sync", None, None)
        c.telegram.bot.send_message.assert_not_called()

    def test_problem_auto_summary_is_single_message_with_at_most_ten_lots(self):
        for notify in (False, True):
            runtime, c = self.runtime(replace(CFG, notify_on_complete=notify))
            report = sync.RunReport("sync")
            for lot_id in range(1, 16):
                report.record(sync.LotResult(lot_id, "dead", sync.ParsedLot(lot_id, 100 + lot_id, 1000, D("10")),
                                             reason="dead service ID"))
            runtime.engine.run.return_value = report
            sync.run_lock.acquire()
            runtime._run(runtime.store.snapshot(), "sync", None, None)
            c.telegram.bot.send_message.assert_called_once()
            text = c.telegram.bot.send_message.call_args.args[1]
            for lot_id in range(1, 11):
                self.assertIn(f"#{lot_id}: Service ID {100 + lot_id}", text)
            self.assertNotIn("#11:", text)
            self.assertIn("ещё 5", text)
            self.assertLess(len(text), 4096)

    def test_problem_summary_stays_within_telegram_limit_and_escapes_reasons(self):
        report = sync.RunReport("sync")
        for lot_id in range(20):
            report.record(sync.LotResult(lot_id, "save_error", reason='<bad>"&' * 50))
        text = sync.format_problem_report(report)
        self.assertNotIn("<bad>", text)
        self.assertIn("&lt;bad&gt;", text)
        self.assertLess(len(text), 4096)

    def test_busy_scheduler_retries_shortly_without_parallel_worker(self):
        runtime, _ = self.runtime(replace(CFG, enabled=True))
        event, timeouts = Mock(), []

        def wait(timeout):
            timeouts.append(timeout)
            if len(timeouts) < 3:
                self.assertTrue(sync.run_lock.locked())
                return False  # Both scheduled attempts encounter the same occupied lock.
            if len(timeouts) == 3:
                sync.run_lock.release()
                return False
            runtime.stop()
            return True

        event.wait.side_effect = wait
        runtime.wake = event
        sync.run_lock.acquire()
        try:
            with patch.object(sync.threading, "Thread") as thread:
                runtime._schedule()
                thread.assert_called_once()  # Only after the lock becomes available.
                thread.return_value.start.assert_called_once()
        finally:
            if sync.run_lock.locked():
                sync.run_lock.release()  # The mocked worker does not execute its finally block.
        self.assertEqual(timeouts, [43200, sync.AUTO_RETRY_DELAY, sync.AUTO_RETRY_DELAY, 43200])
        self.assertTrue(60 <= sync.AUTO_RETRY_DELAY <= 120)

    def test_busy_retry_survives_unrelated_settings_wake_and_can_be_disabled(self):
        runtime, _ = self.runtime(replace(CFG, enabled=True))
        runtime.start_run = Mock(return_value=False)
        event, timeouts = Mock(), []

        def wait(timeout):
            timeouts.append(timeout)
            if len(timeouts) == 1:
                return False
            if len(timeouts) == 2:
                runtime.update_config(threshold=D("0.2"))
            elif len(timeouts) == 3:
                runtime.update_config(enabled=False)
            elif len(timeouts) == 4:
                runtime.update_config(enabled=True)
            else:
                runtime.stop()
            return True

        event.wait.side_effect = wait
        runtime.wake = event
        runtime._schedule()
        self.assertEqual(timeouts, [43200, sync.AUTO_RETRY_DELAY, sync.AUTO_RETRY_DELAY, None, 43200])
        runtime.start_run.assert_called_once_with(automatic=True)

    def test_successful_input_restores_original_menu_message(self):
        for name, value in (("multiplier", "2,5"), ("threshold", "0"), ("update_interval", "1900"), ("api_key", "new-key")):
            with self.subTest(name=name):
                runtime, c = self.runtime()
                runtime.ui.callback(self.call(name))
                c.telegram.bot.reset_mock()
                message = self.message(value)
                message.message_id = 33  # The settings prompt belongs to message 22.
                with patch.object(sync, "SmmWayClient"):
                    runtime.ui.input_message(message)
                c.telegram.bot.send_message.assert_not_called()
                self.assertEqual(c.telegram.bot.edit_message_text.call_args.kwargs["message_id"], 22)
                self.assertIsNone(runtime.ui.input_field(message))
                c.telegram.bot.delete_message.assert_called_once_with(100, 33)
                text = c.telegram.bot.edit_message_text.call_args.args[0]
                self.assertIn("SMMWay AutoSync" if name == "multiplier" else "Настройки AutoSync", text)

    def test_successful_input_falls_back_to_new_menu_if_original_cannot_be_edited(self):
        runtime, c = self.runtime()
        runtime.ui.callback(self.call("threshold"))
        c.telegram.bot.reset_mock()
        c.telegram.bot.edit_message_text.side_effect = RuntimeError("message deleted")
        runtime.ui.input_message(self.message("0"))
        c.telegram.bot.send_message.assert_called_once()
        self.assertEqual(runtime.store.snapshot().threshold, D(0))

    def test_mask_menu_and_callback_sizes(self):
        runtime, c = self.runtime()
        runtime.ui.menu(100)
        args, kwargs = c.telegram.bot.send_message.call_args
        self.assertNotIn(CFG.api_key, args[1])
        for row in kwargs["reply_markup"].keyboard:
            for button in row:
                self.assertLessEqual(len(button.callback_data.encode()), 64)
        for key in ("x", "1234567890", CFG.api_key):
            self.assertNotEqual(sync.mask_key(key), key)

    def test_key_real_validation_before_save_delete_and_old_key_preserved_on_failure(self):
        runtime, c = self.runtime()
        runtime.ui.callback(self.call("api_key"))
        with patch.object(sync, "SmmWayClient") as client:
            client.return_value.get_services.side_effect = sync.SmmWayAPIError("auth failed")
            runtime.ui.input_message(self.message("new-key"))
            self.assertEqual(runtime.store.snapshot().api_key, CFG.api_key)
            self.assertEqual(runtime.ui.input_field(self.message()), "api_key")
            c.telegram.bot.delete_message.assert_called_with(100, 22)
            client.return_value.get_services.side_effect = None
            runtime.ui.input_message(self.message("  short-but-valid  "))
            self.assertEqual(runtime.store.snapshot().api_key, "short-but-valid")
            self.assertIsNone(runtime.ui.input_field(self.message()))

    def test_key_cancellation_during_validation_prevents_save(self):
        runtime, c = self.runtime()
        runtime.ui.callback(self.call("api_key"))
        with patch.object(sync, "SmmWayClient") as client:
            client.return_value.get_services.side_effect = lambda: runtime.ui.callback(self.call("cancel"))
            runtime.ui.input_message(self.message("new-key"))
        self.assertEqual(runtime.store.snapshot().api_key, CFG.api_key)

    def test_cancel_invalid_input_and_unauthorized_user(self):
        runtime, c = self.runtime()
        runtime.ui.callback(self.call("multiplier"))
        runtime.ui.input_message(self.message("NaN"))
        self.assertEqual(runtime.store.snapshot().multiplier, D("1.5"))
        self.assertEqual(runtime.ui.input_field(self.message()), "multiplier")
        runtime.ui.input_message(self.message("2,5", uid=999))
        self.assertEqual(runtime.store.snapshot().multiplier, D("1.5"))
        runtime.ui.callback(self.call("cancel"))
        self.assertIsNone(runtime.ui.input_field(self.message()))
        runtime.ui.callback(self.call("multiplier"))
        runtime.ui.input_message(self.message("/cancel"))
        self.assertIsNone(runtime.ui.input_field(self.message()))

    def test_busy_button_does_not_create_thread(self):
        runtime, c = self.runtime()
        with patch.object(sync.threading, "Thread") as thread:
            sync.run_lock.acquire()
            try:
                runtime.ui.callback(self.call("sync"))
            finally:
                sync.run_lock.release()
            thread.assert_not_called()
        self.assertIn("уже выполняется", c.telegram.bot.answer_callback_query.call_args.args[1])

    def test_no_service_replacement_action_exists(self):
        runtime, c = self.runtime()
        runtime.ui.callback(self.call("replace_1_123_456"))
        c.account.save_lot.assert_not_called()
        self.assertEqual(c.account.lots[1].description_ru, DESCRIPTION)

    def test_telegram_failure_does_not_prevent_run(self):
        runtime, c = self.runtime()
        c.telegram.bot.edit_message_text.side_effect = RuntimeError("Telegram unavailable")
        sync.run_lock.acquire()
        runtime._run(CFG, "sync", 100, 22)
        runtime.engine.run.assert_called_once()
        self.assertFalse(sync.run_lock.locked())
        self.assertTrue(c.telegram.bot.send_message.called)

    def test_telegram_exception_secrets_are_redacted_in_debug_logs(self):
        runtime, c = self.runtime()
        c.telegram.bot.edit_message_text.side_effect = RuntimeError(CFG.api_key)
        with self.assertLogs(sync.logger, level="DEBUG") as logs:
            self.assertFalse(runtime.ui.edit(100, 22, "status"))
        self.assertNotIn(CFG.api_key, " ".join(logs.output))

    def test_dynamic_html_and_message_size(self):
        report = sync.RunReport("dead")
        for number in range(20):
            lot = sync.ParsedLot(number, 999, 1000, D("10"), title='<script>"&' * 30)
            report.record(sync.LotResult(number, "dead", lot, reason='<bad>"&' * 50))
        text = sync.format_report(report)
        self.assertIn("&lt;bad&gt;", text)
        self.assertNotIn("<bad>", text)
        self.assertLess(len(text), 4096)
        pages = sync.dead_id_pages(report)
        self.assertEqual(len(pages), 4)
        for page in pages:
            self.assertNotIn("<script>", page)
            self.assertLess(len(page), 4096)

    def test_intervals_human_readable(self):
        self.assertEqual(sync.format_interval(1900), "31 мин 40 сек")
        self.assertEqual(sync.format_interval(43200), "12 ч")
        self.assertEqual(sync.format_interval(108000), "1 д 6 ч")

    def test_main_menu_is_compact_and_settings_hold_technical_controls(self):
        runtime, c = self.runtime()
        runtime.ui.menu(100)
        text = c.telegram.bot.send_message.call_args.args[1]
        self.assertIn("SMMWay AutoSync", text)
        self.assertIn("🔴 Автосинк выключен", text)
        self.assertIn("Множитель: ×1.5", text)
        for technical in ("API-ключ", "Порог", "Область", "Направление", "Интервал", "Округление", "минимум"):
            self.assertNotIn(technical, text)
        keyboard = c.telegram.bot.send_message.call_args.kwargs["reply_markup"].keyboard
        actions = [[button.callback_data[len(sync.UUID) + 1:] for button in row] for row in keyboard]
        self.assertEqual(actions, [["enabled"], ["multiplier"], ["sync", "preview"], ["dead", "settings"]])
        runtime.ui.settings(100, 1000)
        text = c.telegram.bot.edit_message_text.call_args.args[0]
        self.assertIn("Настройки AutoSync", text)
        self.assertIn(sync.mask_key(CFG.api_key), text)
        self.assertNotIn(CFG.api_key, text)
        self.assertIn("снижение запрещено", text)
        self.assertIn("Все SMMWay — все распознанные", text)
        keyboard = c.telegram.bot.edit_message_text.call_args.kwargs["reply_markup"].keyboard
        actions = {button.callback_data[len(sync.UUID) + 1:] for row in keyboard for button in row}
        self.assertEqual(actions, {"update_interval", "notify_on_complete", "api_key", "direction", "threshold", "scope", "home"})

    def test_home_settings_navigation_edits_one_menu(self):
        runtime, c = self.runtime()
        runtime.ui.command(self.message("/smmsync"))
        menu_id = runtime.ui.menu_messages[100]
        c.telegram.bot.reset_mock()
        for action, title in (("settings", "Настройки AutoSync"), ("home", "SMMWay AutoSync")):
            call = self.call(action)
            call.message.message_id = menu_id
            runtime.ui.callback(call)
            self.assertIn(title, c.telegram.bot.edit_message_text.call_args.args[0])
            self.assertEqual(c.telegram.bot.edit_message_text.call_args.kwargs["message_id"], menu_id)
        c.telegram.bot.send_message.assert_not_called()
        c.telegram.bot.delete_message.assert_not_called()
        self.assertEqual(runtime.ui.menu_messages[100], menu_id)

    def test_settings_toggles_stay_in_settings_and_keep_existing_behavior(self):
        runtime, c = self.runtime()
        for action, field, value in (("notify_on_complete", "notify_on_complete", False),
                                     ("direction", "direction", "up_only"), ("scope", "scope", "all_smmway")):
            runtime.ui.callback(self.call(action))
            self.assertEqual(getattr(runtime.store.snapshot(), field), value)
            self.assertIn("Настройки AutoSync", c.telegram.bot.edit_message_text.call_args.args[0])
        c.telegram.bot.send_message.assert_not_called()

    def test_input_prompts_explain_price_threshold_and_interval(self):
        runtime, c = self.runtime()
        for action, explanations in (("multiplier", ("себестоимости", "+50%", "Цена =")),
                                      ("threshold", ("минимальное изменение", "0 =", "10.05", "10.15", "итоговая цена FunPay")),
                                      ("update_interval", ("Как часто", "секунд", "Сейчас: 12 ч"))):
            with self.subTest(action=action):
                runtime.ui.callback(self.call(action))
                text = c.telegram.bot.edit_message_text.call_args.args[0]
                for explanation in explanations:
                    self.assertIn(explanation, text)
                self.assertEqual(runtime.ui.input_field(self.message()), action)

    def test_invalid_nonsecret_input_is_not_deleted(self):
        runtime, c = self.runtime()
        runtime.ui.callback(self.call("threshold"))
        runtime.ui.input_message(self.message("-1"))
        c.telegram.bot.delete_message.assert_not_called()
        self.assertEqual(runtime.store.snapshot().threshold, CFG.threshold)
        self.assertEqual(runtime.ui.input_field(self.message()), "threshold")

    def test_cancel_returns_to_screen_that_owns_input(self):
        for action in ("multiplier", "threshold", "update_interval", "api_key"):
            for inline in (False, True):
                with self.subTest(action=action, inline=inline):
                    runtime, c = self.runtime()
                    runtime.ui.callback(self.call(action))
                    c.telegram.bot.reset_mock()
                    if inline:
                        runtime.ui.callback(self.call("cancel"))
                    else:
                        runtime.ui.input_message(self.message("/cancel"))
                    self.assertIn("SMMWay AutoSync" if action == "multiplier" else "Настройки AutoSync",
                                  c.telegram.bot.edit_message_text.call_args.args[0])
                    c.telegram.bot.send_message.assert_not_called()
                    self.assertIsNone(runtime.ui.input_field(self.message()))

    def test_smmsync_deletes_previous_menu_and_command_only_in_its_chat(self):
        runtime, c = self.runtime()
        runtime.ui.menu(200)
        other_menu_id = runtime.ui.menu_messages[200]
        runtime.ui.command(self.message("/smmsync"))
        first_id = runtime.ui.menu_messages[100]
        c.telegram.bot.reset_mock()
        command = self.message("/smmsync")
        command.message_id = 44
        runtime.ui.command(command)
        self.assertEqual(c.telegram.bot.delete_message.call_args_list,
                         [unittest.mock.call(100, first_id), unittest.mock.call(100, 44)])
        c.telegram.bot.send_message.assert_called_once()
        self.assertNotEqual(runtime.ui.menu_messages[100], first_id)
        self.assertEqual(runtime.ui.menu_messages[200], other_menu_id)

    def test_unauthorized_command_does_not_delete_or_replace_menus(self):
        runtime, c = self.runtime()
        runtime.ui.menu(100)
        previous = runtime.ui.menu_messages.copy()
        c.telegram.bot.reset_mock()
        runtime.ui.command(self.message("/smmsync", uid=999))
        c.telegram.bot.delete_message.assert_not_called()
        c.telegram.bot.send_message.assert_not_called()
        self.assertEqual(runtime.ui.menu_messages, previous)

    def test_edit_fallback_tracks_new_menu_for_next_command(self):
        runtime, c = self.runtime()
        original_id = runtime.ui.menu(100)
        c.telegram.bot.edit_message_text.side_effect = RuntimeError("message cannot be edited")
        runtime.ui.settings(100, original_id)
        fallback_id = runtime.ui.menu_messages[100]
        self.assertNotEqual(fallback_id, original_id)
        c.telegram.bot.delete_message.assert_called_with(100, original_id)
        c.telegram.bot.reset_mock()
        runtime.ui.command(self.message("/smmsync"))
        self.assertEqual(c.telegram.bot.delete_message.call_args_list[0].args, (100, fallback_id))

    def test_prompt_fallback_updates_state_and_success_edits_new_message(self):
        runtime, c = self.runtime()
        c.telegram.bot.edit_message_text.side_effect = RuntimeError("old menu deleted")
        runtime.ui.callback(self.call("threshold"))
        prompt_id = runtime.ui.menu_messages[100]
        self.assertEqual(c.telegram.get_state(100, 7)["mid"], prompt_id)
        c.telegram.bot.reset_mock()
        c.telegram.bot.edit_message_text.side_effect = None
        value = self.message("0")
        value.message_id = 33
        runtime.ui.input_message(value)
        self.assertEqual(c.telegram.bot.edit_message_text.call_args.kwargs["message_id"], prompt_id)
        self.assertIn("Настройки AutoSync", c.telegram.bot.edit_message_text.call_args.args[0])
        c.telegram.bot.send_message.assert_not_called()
        c.telegram.bot.delete_message.assert_called_once_with(100, 33)

    def test_delete_and_edit_failures_do_not_prevent_settings_save(self):
        runtime, c = self.runtime()
        runtime.ui.menu(100)
        c.telegram.bot.delete_message.side_effect = RuntimeError("delete denied")
        c.telegram.bot.edit_message_text.side_effect = RuntimeError("edit denied")
        runtime.ui.command(self.message("/smmsync"))
        self.assertIn(100, runtime.ui.menu_messages)
        runtime.ui.callback(self.call("multiplier"))
        runtime.ui.input_message(self.message("2"))
        self.assertEqual(runtime.store.snapshot().multiplier, D(2))
        self.assertIsNone(runtime.ui.input_field(self.message()))
        self.assertIn("Множитель: ×2", c.telegram.bot.send_message.call_args.args[1])

    def test_send_failure_does_not_record_phantom_menu_or_crash(self):
        runtime, c = self.runtime()
        c.telegram.bot.send_message.side_effect = RuntimeError("send denied")
        runtime.ui.command(self.message("/smmsync"))
        self.assertNotIn(100, runtime.ui.menu_messages)
        c.telegram.bot.edit_message_text.side_effect = RuntimeError("edit denied")
        runtime.ui.callback(self.call("threshold"))
        runtime.ui.input_message(self.message("0"))
        self.assertEqual(runtime.store.snapshot().threshold, D(0))
        self.assertIsNone(runtime.ui.input_field(self.message()))

    def test_countdown_formats_minutes_hours_days_without_seconds(self):
        for seconds, expected in ((23 * 60, "23 мин"), (2 * 3600 + 14 * 60, "2 ч 14 мин"),
                                  (86400 + 3 * 3600 + 12 * 60, "1 д 3 ч 12 мин"),
                                  (86399, "23 ч 59 мин"), (90, "1 мин"), (59, "менее 1 мин"),
                                  (0, "менее 1 мин"), (-30, "менее 1 мин")):
            with self.subTest(seconds=seconds):
                self.assertEqual(sync.format_countdown(seconds), expected)

    def test_main_countdown_reads_monotonic_deadline_without_resetting_it(self):
        runtime, c = self.runtime(replace(CFG, enabled=True))
        runtime.next_auto_deadline = 1000
        for now, expected in ((700, "5 мин"), (760, "4 мин")):
            with patch.object(sync.time, "monotonic", return_value=now):
                runtime.ui.menu(100, 22)
            self.assertIn("Следующий обход через: " + expected, c.telegram.bot.edit_message_text.call_args.args[0])
            self.assertEqual(runtime.next_auto_deadline, 1000)

    def test_disabled_auto_never_displays_countdown_even_with_stale_deadline(self):
        runtime, c = self.runtime()
        runtime.next_auto_deadline = sync.time.monotonic() + 3600
        runtime.ui.menu(100)
        text = c.telegram.bot.send_message.call_args.args[1]
        self.assertNotIn("Следующий обход", text)
        self.assertIn("Автообход не запланирован", text)

    def test_busy_scheduler_publishes_retry_deadline_used_by_menu(self):
        runtime, c = self.runtime(replace(CFG, enabled=True))
        runtime.start_run = Mock(side_effect=[False, True])
        event, clock, timeouts = Mock(), [100], []

        def wait(timeout):
            timeouts.append(timeout)
            self.assertEqual(runtime.next_auto_deadline, clock[0] + timeout)
            if len(timeouts) == 2:
                clock[0] += 30
                runtime.ui.menu(100, 22)
                text = c.telegram.bot.edit_message_text.call_args.args[0]
                self.assertIn("Следующий обход через: 1 мин", text)
                self.assertIn("Обход выполняется", text)
            if len(timeouts) == 3:
                runtime.stop()
                return True
            return False

        event.wait.side_effect = wait
        runtime.wake = event
        sync.run_lock.acquire()
        try:
            with patch.object(sync.time, "monotonic", side_effect=lambda: clock[0]):
                runtime._schedule()
        finally:
            sync.run_lock.release()
        self.assertEqual(timeouts, [43200, 90, 43200])
        self.assertIsNone(runtime.next_auto_deadline)

    def test_scheduler_recalculates_deadline_on_interval_change_and_disable(self):
        runtime, c = self.runtime(replace(CFG, enabled=True))
        event, clock, deadlines = Mock(), [100], []

        def wait(timeout):
            deadlines.append(runtime.next_auto_deadline)
            if len(deadlines) == 1:
                clock[0] = 150
                runtime.update_config(update_interval=1900)
                self.assertEqual(runtime.next_auto_deadline, 2050)
                runtime.ui.menu(100, 22)
                self.assertIn("Следующий обход через: 31 мин", c.telegram.bot.edit_message_text.call_args.args[0])
            elif len(deadlines) == 2:
                runtime.update_config(enabled=False)
            else:
                runtime.stop()
            return True

        event.wait.side_effect = wait
        runtime.wake = event
        with patch.object(sync.time, "monotonic", side_effect=lambda: clock[0]):
            runtime._schedule()
        self.assertEqual(deadlines, [43300, 2050, None])
        self.assertIsNone(runtime.next_auto_deadline)

    def test_last_run_summary_records_success_problems_and_fatal(self):
        runtime, c = self.runtime(replace(CFG, notify_on_complete=False))
        for problems, fatal, label in ((0, "", "✅ без проблем"), (2, "", "⚠️ проблем: 2"),
                                       (0, "catalog failed", "⚠️ проход прерван")):
            report = sync.RunReport("sync", fatal=fatal)
            report.counts["dead"] = problems
            runtime.engine.run.return_value = report
            sync.run_lock.acquire()
            with patch.object(sync.time, "time", return_value=1234):
                runtime._run(runtime.store.snapshot(), "sync", None, None)
            self.assertEqual(runtime.last_run, {"finished_at": 1234, "fatal": bool(fatal), "problems": problems})
            runtime.ui.menu(100, 22)
            self.assertIn("Последний обход: " + label, c.telegram.bot.edit_message_text.call_args.args[0])

    def test_manual_report_warns_about_real_problems_but_not_normalization(self):
        report = sync.RunReport("sync")
        lot = sync.ParsedLot(1, 123, 1, D("1"))
        report.record(sync.LotResult(1, "updated", lot, D("0.055")))
        self.assertTrue(sync.format_report(report).startswith("✅"))
        report.record(sync.LotResult(2, "dead", reason="dead service ID 456"))
        text = sync.format_report(report)
        self.assertTrue(text.startswith("⚠️"))
        self.assertIn("Обход завершён с предупреждениями", text)

    def test_import_has_no_network_config_writes_or_threads(self):
        name = "autosync_import_probe"
        probe_spec = importlib.util.spec_from_file_location(name, spec.origin)
        module = importlib.util.module_from_spec(probe_spec)
        sys.modules[name] = module
        try:
            with patch.dict(sys.modules, dependencies), patch.object(threading, "Thread") as thread, patch.object(Path, "open") as file_open:
                probe_spec.loader.exec_module(module)
                thread.assert_not_called()
                file_open.assert_not_called()
                self.assertIsNone(module._runtime)
        finally:
            sys.modules.pop(name)

    def test_init_is_idempotent(self):
        with patch.object(sync, "_runtime", None), patch.object(sync, "PluginRuntime") as runtime:
            c = cardinal()
            sync.init(c)
            sync.init(c)
            runtime.assert_called_once_with(c)
            runtime.return_value.start_scheduler.assert_called_once()
            runtime.return_value.ui.register.assert_called_once()


if __name__ == "__main__":
    unittest.main()
