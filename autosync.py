from __future__ import annotations

import html
import json
import logging
import os
import random
import re
import threading
import time
import traceback
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from decimal import Decimal, InvalidOperation, ROUND_UP, localcontext
from pathlib import Path
from typing import TYPE_CHECKING

import requests
from FunPayAPI.common import exceptions as fp_exceptions
from telebot.types import InlineKeyboardButton as B, InlineKeyboardMarkup as K

if TYPE_CHECKING:
    from cardinal import Cardinal

NAME = "SMMWay Price AutoSync"
VERSION = "2.1.0"
DESCRIPTION = "Безопасная синхронизация цен SMMWay, предпросмотр и диагностика мёртвых ID."
CREDITS = "@speedybon"
UUID = "a207b8bc-d7ec-4fac-ab7e-cb7d2b1cfaf2"
SETTINGS_PAGE = False
GITHUB = "https://github.com/speedyticket/smmway-autosync-cardinal"
logger = logging.getLogger("FPC.smmway_autosync")

CONFIG_FILE = Path("storage/smmway_autosync/config.json")
MIN_INTERVAL, MAX_INTERVAL = 1900, 7 * 86400
PRICE_DECIMALS, PRICE_ROUNDING = 3, ROUND_UP
STEP_DELAY, SMM_ATTEMPTS, FUNPAY_ATTEMPTS = 0.75, 6, 4
AUTO_RETRY_DELAY = 90
RETRY_STATUSES = {429, 500, 502, 503, 504}
PROBLEM_STATUSES = {"dead", "malformed", "invalid_price", "read_error", "save_error", "changed"}
SMMWAY_URL = "https://smmway.ru/api/v2"
SMMWAY_ALIASES = {"way", "smmway"}  # AutoSMM's never/neversmm aliases are deliberately excluded.
run_lock = threading.Lock()
_runtime = None


def decimal_value(value) -> Decimal:
    try:
        result = Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        raise ValueError("Ожидается число, например 1.5 или 1,5.") from None
    if not result.is_finite():
        raise ValueError("Число должно быть конечным.")
    return result


def safe_error(error, secret="") -> str:
    text = str(error)
    return text.replace(secret, "[ключ скрыт]") if secret else text


def log_exception(message, secret="", level=logging.ERROR):
    # Also redact chained HTTP exceptions while retaining their stack traces in logs.
    if logger.isEnabledFor(level):
        logger.log(level, "%s\n%s", message, safe_error(traceback.format_exc(), secret))


def escape(value, limit=160) -> str:
    return html.escape(str(value)[:limit])


def mask_key(key: str) -> str:
    return f"{key[:6]}...{key[-4:]}" if len(key) > 10 else ("••••" if key else "не задан")


@dataclass(frozen=True)
class PluginConfig:
    enabled: bool = False
    api_key: str = field(default="", repr=False)
    multiplier: Decimal = Decimal("1.5")
    update_interval: int = 43200
    threshold: Decimal = Decimal("0.1")
    direction: str = "both"
    scope: str = "marked_only"
    # Legacy fields remain readable; pricing always uses FunPay's fixed 3-digit ROUND_UP format.
    rounding: str = "up"
    price_decimals: int = PRICE_DECIMALS
    min_price: Decimal = Decimal("0.01")
    notify_on_complete: bool = True
    run_on_start: bool = False


DEFAULT_CONFIG = asdict(PluginConfig())


def validate_setting(name, value):
    if name in {"enabled", "notify_on_complete", "run_on_start"}:
        if type(value) is not bool:
            raise ValueError("Ожидается true или false.")
    elif name == "api_key":
        if not isinstance(value, str):
            raise ValueError("Ключ должен быть строкой.")
        value = value.strip()
    elif name in {"multiplier", "threshold", "min_price"}:
        value = decimal_value(value)
        maximum = Decimal("500") if name == "multiplier" else Decimal("1000000")
        if value < 0 or value > maximum or (name != "threshold" and value == 0):
            raise ValueError(f"Допустимо {'0 ≤' if name == 'threshold' else '0 <'} число ≤ {maximum}.")
    elif name in {"update_interval", "price_decimals"}:
        number = decimal_value(value)
        low, high = (MIN_INTERVAL, MAX_INTERVAL) if name == "update_interval" else (0, 8)
        if number != number.to_integral_value() or not low <= number <= high:
            raise ValueError(f"Введите целое число от {low} до {high}.")
        value = int(number)
    elif name in {"direction", "scope", "rounding"}:
        choices = {"direction": ("both", "up_only"), "scope": ("marked_only", "all_smmway"), "rounding": ("up", "down")}
        if value not in choices[name]:
            raise ValueError("Неизвестный режим.")
    else:
        raise ValueError("Неизвестная настройка.")
    return value


def validate_config(raw) -> PluginConfig:
    values = dict(DEFAULT_CONFIG)
    if isinstance(raw, dict):
        for name in values:
            if name in raw:
                try:
                    values[name] = validate_setting(name, raw[name])
                except (ValueError, TypeError):
                    logger.warning("Некорректная настройка %s: используется default", name)
    return PluginConfig(**values)


def read_config_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8-sig") as stream:
            data = json.load(stream)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        logger.warning("Не удалось прочитать config %s; используются безопасные defaults", path)
    return {}


class ConfigStore:
    def __init__(self, path=CONFIG_FILE, legacy=None, shadow=None):
        self.path = Path(path)
        self.lock = threading.Lock()
        is_new = not self.path.exists()
        if not is_new:
            raw = read_config_file(self.path)  # Existing config wins, even if damaged.
        else:
            legacy = Path(legacy) if legacy is not None else Path(__file__).with_name("smm_config.json")
            shadow = Path(shadow) if shadow is not None else Path("storage/shadowcent/smm/smmway_state.json")
            raw = read_config_file(legacy)
            old_shadow = read_config_file(shadow)
            for old, new in {"auto_interval_sec": "update_interval", "last_mult": "multiplier",
                             "notify_on_complete": "notify_on_complete", "api_key": "api_key"}.items():
                if new not in raw and old_shadow.get(old) is not None:
                    raw[new] = old_shadow[old]
        self._config = validate_config(raw)
        if is_new:
            self._save(self._config)
            logger.info("Config %s: %s", self.path, "миграция завершена" if raw else "созданы defaults")

    def snapshot(self) -> PluginConfig:
        with self.lock:
            return self._config  # Frozen config contains only immutable values.

    def _save(self, cfg):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {key: str(value) if isinstance(value, Decimal) else value for key, value in asdict(cfg).items()}
        temporary = self.path.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)

    def update(self, **changes):
        changes = {key: validate_setting(key, value) for key, value in changes.items()}
        with self.lock:
            cfg = replace(self._config, **changes)
            self._save(cfg)
            self._config = cfg
        logger.info("Настройки изменены: %s", ", ".join(changes))  # Names only, never secrets.


def backoff(attempt, retry_after=None):
    delay = min(2 ** (attempt - 1), 16)
    if retry_after:
        try:
            delay = max(delay, float(min(Decimal("30"), max(Decimal(0), decimal_value(retry_after)))))
        except ValueError:
            pass  # A date or invalid Retry-After falls back to bounded exponential backoff.
    return delay + random.uniform(0, 0.25)


@dataclass(frozen=True)
class SmmService:
    service_id: int
    name: str
    category: str
    rate: Decimal
    minimum: int | None = None
    maximum: int | None = None


class SmmWayAPIError(Exception):
    pass


class SmmWayClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()

    def close(self):
        self.session.close()

    def get_services(self) -> dict[int, SmmService]:
        if not self.api_key:
            raise SmmWayAPIError("API-ключ не задан.")
        for attempt in range(1, SMM_ATTEMPTS + 1):
            try:
                response = self.session.post(SMMWAY_URL, data={"action": "services", "key": self.api_key},
                                             timeout=(5, 20), allow_redirects=False)
            except (requests.Timeout, requests.ConnectionError) as error:
                if attempt == SMM_ATTEMPTS:
                    raise SmmWayAPIError("Сеть SMMWay недоступна; попытки исчерпаны.") from None
                logger.warning("SMMWay %s; повтор %s/%s", type(error).__name__, attempt, SMM_ATTEMPTS)
                time.sleep(backoff(attempt))
                continue
            except requests.RequestException:
                raise SmmWayAPIError("Не удалось выполнить запрос SMMWay.") from None
            if response.status_code in RETRY_STATUSES:
                if attempt == SMM_ATTEMPTS:
                    raise SmmWayAPIError(f"SMMWay HTTP {response.status_code}; попытки исчерпаны.")
                logger.warning("SMMWay HTTP %s; повтор %s/%s", response.status_code, attempt, SMM_ATTEMPTS)
                time.sleep(backoff(attempt, response.headers.get("Retry-After")))
                continue
            if response.status_code != 200:
                raise SmmWayAPIError(f"SMMWay HTTP {response.status_code}. Проверьте доступ и API-ключ.")
            try:
                data = response.json(parse_float=Decimal)
            except ValueError:
                raise SmmWayAPIError("SMMWay вернул некорректный JSON.") from None
            if isinstance(data, dict) and "error" in data:
                error = safe_error(data["error"], self.api_key)
                message = {"user_inactive": "Аккаунт SMMWay не активирован или заблокирован.",
                           "invalid_key": "SMMWay отклонил API-ключ."}.get(error, f"Ошибка API SMMWay: {error[:160]}")
                raise SmmWayAPIError(message)
            if not isinstance(data, list) or not data:
                raise SmmWayAPIError("SMMWay вернул пустой или некорректный каталог.")
            catalog, unusable_ids = {}, set()
            for index, item in enumerate(data, 1):
                sid = None
                try:
                    sid = positive_integer(str(item["service"]))
                    if sid in unusable_ids:
                        continue  # A later duplicate must never revive an ambiguous/broken ID.
                    rate = decimal_value(item["rate"])
                    if rate <= 0:
                        raise ValueError("rate ≤ 0")
                    service = SmmService(sid, str(item.get("name", "")), str(item.get("category", "")), rate,
                                         int(item["min"]) if item.get("min") is not None else None,
                                         int(item["max"]) if item.get("max") is not None else None)
                    if sid in catalog and catalog[sid] != service:
                        raise ValueError("Конфликт ID в каталоге")
                    catalog[sid] = service
                except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as error:
                    if sid is not None:
                        unusable_ids.add(sid)
                        catalog.pop(sid, None)
                    logger.warning("SMMWay: запись %s (ID %s) исключена: %s", index, sid,
                                   safe_error(error, self.api_key)[:160])
            if not catalog:
                raise SmmWayAPIError("После проверки каталога SMMWay не осталось валидных услуг.")
            return catalog


@dataclass(frozen=True)
class ParsedLot:
    lot_id: int
    service_id: int
    amount: int
    current_price: Decimal
    source: str = "description_ru"
    marked: bool = False
    title: str = ""


@dataclass
class LotResult:
    lot_id: int
    status: str
    lot: ParsedLot | None = None
    new_price: Decimal | None = None
    reason: str = ""


KEY_LINE = re.compile(r"^[ \t]*([a-zа-яё_\-]+)[ \t]*[:=][ \t]*(.*?)[ \t]*$", re.IGNORECASE)
ID_KEYS = {"id", "service_id", "service"}
AMOUNT_KEYS = {"am", "nam", "quantity", "qty", "количество"}
PROVIDER_KEYS = {"name", "service_name", "provider"}


def positive_integer(value):
    if not re.fullmatch(r"[0-9]+", value) or int(value) <= 0:
        raise ValueError("ID и количество должны быть положительными целыми числами.")
    return int(value)


def unique_value(pairs, keys, convert, default=None):
    values = {convert(value) for key, value in pairs if key in keys}
    if len(values) > 1:
        raise ValueError("Конфликтующие значения: " + "/".join(sorted(keys)))
    return next(iter(values), default)


def parse_lot(lot_id, description, price, scope="marked_only", source="description_ru") -> LotResult:
    pairs = [(match[1].lower(), match[2].strip()) for line in (description or "").splitlines()
             if (match := KEY_LINE.fullmatch(line))]
    markers = {value.lower() for key, value in pairs if key == "smm"}
    if "off" in markers:
        return LotResult(lot_id, "off", reason="smm: off")
    if not any(key in ID_KEYS or key == "smm" for key, _ in pairs):
        return LotResult(lot_id, "unrelated")
    providers = {"smmway" if value.lower() in SMMWAY_ALIASES else value.lower()
                 for key, value in pairs if key in PROVIDER_KEYS}
    if providers - {"smmway"}:
        return LotResult(lot_id, "provider", reason="Провайдер явно отличается от SMMWay.")
    try:
        if markers - {"on"}:
            raise ValueError("Допустимые значения smm: on или off.")
        sid = unique_value(pairs, ID_KEYS, positive_integer)
        if sid is None:
            raise ValueError("Не указан service ID.")
        amount = unique_value(pairs, AMOUNT_KEYS, positive_integer, 1)
        lot = ParsedLot(lot_id, sid, amount, decimal_value(price), source, "on" in markers)
        if lot.current_price < 0:
            raise ValueError("Текущая цена отрицательна.")
    except ValueError as error:
        return LotResult(lot_id, "malformed", reason=str(error))
    return LotResult(lot_id, "marker" if scope == "marked_only" and not lot.marked else "matched", lot)


def parse_fields(lot_id, fields, scope) -> LotResult:
    results = [parse_lot(lot_id, getattr(fields, source, ""), fields.price, "all_smmway", source)
               for source in ("description_ru", "description_en")]
    # Never bypass a prohibition, foreign provider or malformed block through English.
    for status in ("off", "provider", "malformed"):
        for result in results:
            if result.status == status:
                return result
    matches = [result for result in results if result.lot]
    if not matches:
        return results[0]
    if len({(result.lot.service_id, result.lot.amount) for result in matches}) > 1:
        return LotResult(lot_id, "malformed", reason="Конфликтующие ID/количество в RU и EN описаниях.")
    selected = next((result for result in matches if scope != "marked_only" or result.lot.marked), matches[0])
    selected.lot = replace(selected.lot, title=getattr(fields, "title_ru", "") or getattr(fields, "title_en", "") or "")
    if scope == "marked_only" and not selected.lot.marked:
        selected.status = "marker"
    return selected


def calculate_new_price(rate, amount, cfg: PluginConfig) -> Decimal:
    rate = decimal_value(rate)
    if rate <= 0 or amount <= 0 or cfg.multiplier <= 0:
        raise ValueError("rate, количество и множитель должны быть положительными.")
    with localcontext() as context:
        context.prec = 40
        raw_price = rate / Decimal("1000") * Decimal(str(amount)) * cfg.multiplier
        price = raw_price.quantize(Decimal(1).scaleb(-PRICE_DECIMALS), rounding=PRICE_ROUNDING)
    if price <= 0:
        raise ValueError("Цена должна быть положительной; сохранение отменено.")
    return price


def evaluate_price(lot, service, cfg) -> LotResult:
    if service is None:
        return LotResult(lot.lot_id, "dead", lot, reason=f"dead service ID: {lot.service_id}")
    try:
        price = calculate_new_price(service.rate, lot.amount, cfg)
        # Preview must reject the same unrepresentable prices as the FunPay save boundary.
        if decimal_value(float(price)) != price:
            raise ValueError("Цена теряет точность при передаче FunPay; сохранение отменено.")
    except (ValueError, InvalidOperation, OverflowError) as error:
        return LotResult(lot.lot_id, "invalid_price", lot, reason=str(error))
    with localcontext() as context:
        context.prec = 40
        difference = price - lot.current_price
    status = "updated"
    if difference == 0:
        status = "unchanged"
    elif cfg.direction == "up_only" and difference < 0:
        status = "up_only"
    elif difference.copy_abs() < cfg.threshold:
        status = "threshold"
    return LotResult(lot.lot_id, status, lot, price)


def funpay_retry(function, *args):
    for attempt in range(1, FUNPAY_ATTEMPTS + 1):
        try:
            return function(*args)
        except (fp_exceptions.RequestFailedError, requests.Timeout, requests.ConnectionError) as error:
            status = getattr(error, "status_code", None)
            if status is None:
                status = getattr(getattr(error, "response", None), "status_code", None)
            if (status not in RETRY_STATUSES and not isinstance(error, (requests.Timeout, requests.ConnectionError))) or attempt == FUNPAY_ATTEMPTS:
                raise
            logger.warning("FunPay %s; повтор %s/%s", status or type(error).__name__, attempt, FUNPAY_ATTEMPTS)
            time.sleep(backoff(attempt))


def save_price(account, expected: ParsedLot, service, cfg) -> LotResult:
    def attempt():
        fields = account.get_lot_fields(expected.lot_id)
        current = parse_fields(expected.lot_id, fields, cfg.scope)
        lot = current.lot
        if current.status != "matched" or (lot.service_id, lot.amount, lot.source) != (
                expected.service_id, expected.amount, expected.source):
            return LotResult(expected.lot_id, "changed", expected, reason="Лот изменён во время обхода; сохранение отменено.")
        result = evaluate_price(lot, service, cfg)  # Direction/threshold use the freshly fetched price.
        if result.status == "updated":
            # FunPay examples use float fields; only this boundary converts the finished Decimal.
            fields.price = float(result.new_price)
            fields.csrf_token = account.csrf_token
            fields.renew_fields()
            account.save_lot(fields)
        return result
    return funpay_retry(attempt)


@dataclass
class RunReport:
    mode: str
    total: int = 0
    counts: Counter = field(default_factory=Counter)
    results: list[LotResult] = field(default_factory=list)
    fatal: str = ""

    def record(self, result):
        self.results.append(result)
        self.counts["scanned"] += 1
        self.counts[result.status] += 1
        if result.lot is not None and result.status != "marker":
            self.counts["matched"] += 1
        if result.status == "updated":
            self.counts["increased" if result.new_price > result.lot.current_price else "decreased"] += 1


class SyncEngine:
    def __init__(self, cardinal, client_factory=SmmWayClient):
        self.cardinal = cardinal
        self.client_factory = client_factory

    def run(self, cfg, mode="sync", progress=None) -> RunReport:
        report = RunReport(mode)
        logger.info("Начало обхода: %s", mode)
        client = None
        try:
            try:
                client = self.client_factory(cfg.api_key)
                services = client.get_services()  # One catalog per pass, also for preview/dead diagnostics.
            except Exception as error:
                log_exception("Не удалось получить каталог SMMWay", cfg.api_key)
                report.fatal = "Не удалось получить каталог SMMWay. Цены лотов не изменялись. " + safe_error(error, cfg.api_key)[:160]
                return report
            funpay_retry(self.cardinal.update_lots_and_categories)
            lots = {lot.id: lot for lot in self.cardinal.tg_profile.get_common_lots()}
            report.total = len(lots)
            for index, lot_id in enumerate(lots, 1):
                try:
                    fields = funpay_retry(self.cardinal.account.get_lot_fields, lot_id)
                except Exception as error:
                    log_exception(f"FunPay: чтение лота #{lot_id}", cfg.api_key)
                    result = LotResult(lot_id, "read_error", reason=safe_error(error, cfg.api_key)[:160])
                else:
                    try:
                        result = self.process_lot(lot_id, fields, services, cfg, mode)
                    except Exception as error:
                        log_exception(f"Обработка лота #{lot_id}", cfg.api_key)
                        result = LotResult(lot_id, "malformed", reason=safe_error(error, cfg.api_key)[:160])
                report.record(result)
                if progress and (index == 1 or index % 5 == 0 or index == report.total):
                    try:
                        progress(report)
                    except Exception:
                        log_exception("Не удалось показать прогресс Telegram", cfg.api_key, logging.DEBUG)
                if index < report.total:
                    time.sleep(STEP_DELAY)
        except Exception as error:
            log_exception("Обход прерван", cfg.api_key)
            report.fatal = "Обход прерван; часть лотов могла быть обновлена. " + safe_error(error, cfg.api_key)[:160]
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    log_exception("Не удалось закрыть Session SMMWay", cfg.api_key, logging.DEBUG)
            logger.info("Конец обхода %s: %s; fatal=%s", mode, dict(report.counts), bool(report.fatal))
        return report

    def process_lot(self, lot_id, fields, services, cfg, mode):
        result = parse_fields(lot_id, fields, cfg.scope)
        if result.status != "matched":
            if result.status == "malformed":
                logger.warning("Некорректное описание лота #%s: %s", lot_id, result.reason)
            return result
        result = evaluate_price(result.lot, services.get(result.lot.service_id), cfg)
        if result.status == "dead":
            logger.warning("Лот #%s: dead service ID %s", lot_id, result.lot.service_id)
        if result.status == "updated" and mode == "sync":
            try:
                result = save_price(self.cardinal.account, result.lot, services[result.lot.service_id], cfg)
            except Exception as error:
                log_exception(f"FunPay: сохранение лота #{lot_id}", cfg.api_key, logging.WARNING)
                result.status, result.reason = "save_error", safe_error(error, cfg.api_key)[:160]
        return result


REPORT_LABELS = (("scanned", "Просмотрено"), ("matched", "Подходящих SMMWay"), ("updated", "Обновлено"),
                 ("increased", "Повышений"), ("decreased", "Снижений"), ("unchanged", "Уже совпадают"),
                 ("threshold", "Ниже порога"), ("up_only", "Запрет снижения"), ("provider", "Другой провайдер"),
                 ("marker", "Нет smm: on"), ("off", "Явный smm: off"), ("unrelated", "Не SMM-лот"),
                 ("malformed", "Некорректное описание"), ("dead", "Мёртвый service ID"),
                 ("invalid_price", "Недопустимая цена"), ("changed", "Изменён во время обхода"),
                 ("read_error", "Ошибки чтения FunPay"), ("save_error", "Ошибки сохранения FunPay"))


def fit_telegram_lines(lines):
    result = []
    size = 0
    for line in lines:
        if size + len(line) + 1 > 3800:
            result.append("<i>… остальные записи в logs.</i>")
            break
        result.append(line)
        size += len(line) + 1
    return "\n".join(result)


def format_report(report):
    if report.fatal and not report.counts["scanned"]:
        return "❌ " + escape(report.fatal, 500)  # No misleading "updated: 0" on catalog failure.
    title = {"sync": "Обход завершён", "preview": "Предпросмотр — цены не сохранялись", "dead": "Диагностика — цены не сохранялись"}[report.mode]
    has_problems = any(report.counts[status] for status in PROBLEM_STATUSES)
    if report.mode == "sync" and has_problems and not report.fatal:
        title += " с предупреждениями"
    icon = "❌" if report.fatal else ("⚠️" if has_problems else "✅")
    lines = [f"{icon} <b>{title}</b>"]
    if report.fatal:
        lines.append(escape(report.fatal, 250))
    for key, label in REPORT_LABELS:
        if key == "updated" and report.mode != "sync":
            label = "Изменились бы"
        lines.append(f"{label}: <b>{report.counts[key]}</b>")
    changes = [result for result in report.results if result.status == "updated"]
    if changes:
        lines.append("\n<b>Первые изменения:</b>")
        lines.extend(f"#{result.lot_id}: {escape(result.lot.current_price, 40)} → {escape(result.new_price, 40)}" for result in changes[:15])
    errors = [result for result in report.results if result.reason and result.status not in {"provider", "off"}]
    if errors:
        lines.append("\n<b>Диагностика (до 10 записей; подробности в logs):</b>")
        lines.extend(f"#{result.lot_id}: {escape(result.reason, 100)}" for result in errors[:10])
    return fit_telegram_lines(lines)


def format_problem_report(report):
    lines = ["⚠️ <b>Автообход SMMWay: обнаружены проблемы</b>"]
    if report.fatal:
        lines.append("❌ " + escape(report.fatal, 250))
    if report.counts["scanned"]:
        lines.append(f"Просмотрено: <b>{report.counts['scanned']}</b>; "
                     f"обновлено: <b>{report.counts['updated']}</b>")
    for status, label in REPORT_LABELS:
        if status in PROBLEM_STATUSES and report.counts[status]:
            lines.append(f"{label}: <b>{report.counts[status]}</b>")
    problems = [result for result in report.results if result.status in PROBLEM_STATUSES]
    if problems:
        lines.append("\n<b>Проблемные лоты (до 10; подробности в logs):</b>")
        for result in problems[:10]:
            reason = result.reason or result.status
            if result.status == "dead" and result.lot:
                reason = f"Service ID {result.lot.service_id}: {reason}"
            lines.append(f"#{escape(result.lot_id)}: {escape(reason, 120)}")
        if len(problems) > 10:
            lines.append(f"<i>… ещё {len(problems) - 10} проблемных лотов.</i>")
    return fit_telegram_lines(lines)


def dead_id_pages(report):
    entries = [result.lot for result in report.results if result.status == "dead"]
    pages = []
    for offset in range(0, len(entries), 5):
        lines = ["☠️ <b>Мёртвые ID — требуется ручной выбор услуги</b>"]
        for lot in entries[offset:offset + 5]:
            lines.append(f"\nFunPay #{lot.lot_id}: {escape(lot.title, 60)}\n"
                         f"Service ID: <code>{lot.service_id}</code>; количество: {lot.amount}; "
                         f"цена: {escape(lot.current_price, 40)}\nПоле: {escape(lot.source)}")
        pages.append("\n".join(lines))
    return pages


def format_interval(seconds):
    parts = []
    for unit, label in ((86400, "д"), (3600, "ч"), (60, "мин"), (1, "сек")):
        count, seconds = divmod(seconds, unit)
        if count:
            parts.append(f"{count} {label}")
    return " ".join(parts) or "0 сек"


def format_countdown(seconds):
    minutes = max(0, int(seconds) // 60)
    return format_interval(minutes * 60) if minutes else "менее 1 мин"


def progress_text(report):
    count = report.counts
    errors = sum(count[key] for key in ("malformed", "invalid_price", "changed", "read_error", "save_error"))
    verb = "изменено" if report.mode == "sync" else "изменились бы"
    return (f"⏳ <b>SMMWay {count['scanned']}/{report.total}</b>\n"
            f"✅ {verb}: {count['updated']}\n⏹ без изменений: {count['unchanged'] + count['threshold']}\n"
            f"⚠️ ошибок: {errors}\n☠️ dead ID: {count['dead']}")


class PluginRuntime:
    def __init__(self, cardinal, store=None):
        self.store = store if store is not None else ConfigStore()
        self.engine = SyncEngine(cardinal)
        self.ui = TelegramUI(cardinal, self)
        self.wake = threading.Event()
        self.stopped = False
        self.scheduler = None
        self.worker = None
        self.next_auto_deadline = None
        self._retry_pending = False
        self.last_run = None

    def start_run(self, mode="sync", chat_id=None, message_id=None, automatic=False):
        if self.stopped or not run_lock.acquire(blocking=False):
            return False
        try:
            cfg = self.store.snapshot()
            if self.stopped or (automatic and (not cfg.enabled or not cfg.api_key)):
                run_lock.release()
                return False
            self.worker = threading.Thread(target=self._run, args=(cfg, mode, chat_id, message_id),
                                           name="smmway-sync", daemon=True)
            self.worker.start()
        except Exception:
            run_lock.release()
            raise
        return True

    def _run(self, cfg, mode, chat_id, message_id):
        try:
            if chat_id is not None:
                self.ui.edit(chat_id, message_id, "⏳ <b>SMMWay — загрузка каталога…</b>")
            report = self.engine.run(cfg, mode, (lambda report: self.ui.edit(chat_id, message_id, progress_text(report)))
                                     if chat_id is not None else None)
            problems = sum(report.counts[status] for status in PROBLEM_STATUSES)
            self.last_run = {"finished_at": time.time(), "fatal": bool(report.fatal), "problems": problems}
            has_problems = bool(report.fatal) or problems > 0
            text = format_problem_report(report) if chat_id is None and has_problems else format_report(report)
            if chat_id is not None:
                if not self.ui.edit(chat_id, message_id, text):
                    self.ui.send(chat_id, text)
                if mode == "dead":
                    for page in dead_id_pages(report):
                        self.ui.send(chat_id, page)
            elif cfg.notify_on_complete or has_problems:
                # Only ordinary successful reports can be muted; every problem gets one summary.
                for uid in self.ui.tg.authorized_users:
                    self.ui.send(uid, text)
        except Exception:
            log_exception("Неожиданная ошибка background worker", cfg.api_key)
            self.last_run = {"finished_at": time.time(), "fatal": True, "problems": 0}
            if chat_id is not None:
                self.ui.send(chat_id, "❌ Обход прерван. Подробности в logs.")
            else:
                for uid in self.ui.tg.authorized_users:
                    self.ui.send(uid, "❌ Автоматический обход прерван. Подробности в logs.")
        finally:
            run_lock.release()

    def start_scheduler(self):
        if self.scheduler is None:
            self.scheduler = threading.Thread(target=self._schedule, name="smmway-scheduler", daemon=True)
            self.scheduler.start()

    def _schedule(self):
        first = True
        while not self.stopped:
            self.wake.clear()  # Clear before snapshot so concurrent settings changes are not lost.
            cfg = self.store.snapshot()
            if self.stopped:
                return
            if not cfg.enabled or not cfg.api_key:
                self._retry_pending = False
            timeout = AUTO_RETRY_DELAY if self._retry_pending else (0 if first and cfg.run_on_start else cfg.update_interval)
            first = False
            self.next_auto_deadline = time.monotonic() + timeout if cfg.enabled and cfg.api_key else None
            if self.wake.wait(timeout if cfg.enabled and cfg.api_key else None):
                continue
            self.next_auto_deadline = None
            current = self.store.snapshot()
            if not self.stopped and current.enabled and current.api_key:
                try:
                    self._retry_pending = not self.start_run(automatic=True)
                    if self._retry_pending:
                        logger.info("Автообход отложен; повтор запуска через %s сек", AUTO_RETRY_DELAY)
                except Exception:
                    log_exception("Не удалось запустить автоматический обход", current.api_key)

    def update_config(self, **changes):
        self.store.update(**changes)
        cfg = self.store.snapshot()
        if not cfg.enabled or not cfg.api_key:
            self._retry_pending = False
        delay = AUTO_RETRY_DELAY if self._retry_pending else cfg.update_interval
        self.next_auto_deadline = time.monotonic() + delay if cfg.enabled and cfg.api_key and not self.stopped else None
        self.wake.set()

    def stop(self):
        self.stopped = True
        self.next_auto_deadline = None
        self.wake.set()


INPUT_PROMPTS = {"multiplier": "Множитель определяет наценку относительно себестоимости SMMWay.\n"
                              "Цена = себестоимость × множитель.\nНапример, 1.5 = +50% к себестоимости.\n\n"
                              "Введите новое значение, например 1.5 (0 < число ≤ 500).",
                 "threshold": "Порог — минимальное изменение цены, ради которого лот будет сохранён.\n"
                              "0 = обновлять при любом изменении.\n"
                              "При пороге 0.1 изменение с 10.00 до 10.05 будет проигнорировано, "
                              "а до 10.15 — применено. Сравнивается итоговая цена FunPay.\n\n"
                              "Введите порог в ₽: от 0 до 1000000, например 0.1.",
                 "update_interval": "Как часто выполнять автоматический обход.\nМожно указать количество секунд.\n\n"
                                    f"Введите интервал: от {MIN_INTERVAL} до {MAX_INTERVAL} секунд.",
                 "api_key": "Отправьте новый API-ключ SMMWay. Он будет проверен запросом services."}


class TelegramUI:
    def __init__(self, cardinal, runtime):
        self.cardinal, self.runtime = cardinal, runtime
        self.tg, self.bot = cardinal.telegram, cardinal.telegram.bot
        self.menu_messages = {}  # Only messages created/edited by this plugin, kept per chat.

    def send(self, chat_id, text, markup=None):
        try:
            return self.bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)
        except Exception:
            log_exception("Telegram send failed", self.runtime.store.snapshot().api_key, logging.DEBUG)

    def edit(self, chat_id, message_id, text, markup=None):
        try:
            self.bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, parse_mode="HTML", reply_markup=markup)
            return True
        except Exception:
            log_exception("Telegram edit failed", self.runtime.store.snapshot().api_key, logging.DEBUG)
            return False

    def delete(self, chat_id, message_id):
        if message_id is None:
            return
        try:
            self.bot.delete_message(chat_id, message_id)
        except Exception:
            log_exception("Telegram delete failed", self.runtime.store.snapshot().api_key, logging.DEBUG)

    def show_menu(self, chat_id, message_id, text, keyboard):
        if message_id is not None and self.edit(chat_id, message_id, text, keyboard):
            self.menu_messages[chat_id] = message_id
            return message_id
        message = self.send(chat_id, text, keyboard)
        if message is not None:
            previous = self.menu_messages.get(chat_id)
            if previous is not None and previous != message.message_id:
                self.delete(chat_id, previous)
            self.menu_messages[chat_id] = message.message_id
            return message.message_id

    def answer(self, call, text=""):
        try:
            self.bot.answer_callback_query(call.id, text)
        except Exception:
            log_exception("Telegram callback answer failed", self.runtime.store.snapshot().api_key, logging.DEBUG)

    def menu(self, chat_id, message_id=None):
        cfg = self.runtime.store.snapshot()
        lines = ["⚙️ <b>SMMWay AutoSync</b>\n", f"{'🟢 Автосинк включён' if cfg.enabled else '🔴 Автосинк выключен'}",
                 f"💹 Множитель: ×{escape(cfg.multiplier, 40)}"]
        if run_lock.locked():
            lines.append("⏳ Обход выполняется")
        deadline = self.runtime.next_auto_deadline
        if cfg.enabled and cfg.api_key and deadline is not None and not self.runtime.stopped:
            lines.append("⏱ Следующий обход через: " + format_countdown(deadline - time.monotonic()))
        elif not cfg.enabled or self.runtime.stopped:
            lines.append("Автообход не запланирован")
        elif not cfg.api_key:
            lines.append("Для автообхода укажите API-ключ в настройках")
        else:
            lines.append("⏱ Планирование автообхода…")
        last = self.runtime.last_run
        if last is not None:
            status = "⚠️ проход прерван" if last["fatal"] else (
                f"⚠️ проблем: {last['problems']}" if last["problems"] else "✅ без проблем")
            lines.append("\nПоследний обход: " + status)
        keyboard = K()
        for row in ((("🔴 Выключить авто" if cfg.enabled else "🟢 Включить авто", "enabled"),),
                    (("💹 Множитель", "multiplier"),),
                    (("▶️ Запустить сейчас", "sync"), ("👁 Предпросмотр", "preview")),
                    (("☠️ Мёртвые ID", "dead"), ("⚙️ Настройки", "settings"))):
            keyboard.row(*(B(label, callback_data=f"{UUID}_{action}") for label, action in row))
        return self.show_menu(chat_id, message_id, "\n".join(lines), keyboard)

    def settings(self, chat_id, message_id=None):
        cfg = self.runtime.store.snapshot()
        direction = "В обе стороны" if cfg.direction == "both" else "Только вверх"
        scope = "Только smm: on" if cfg.scope == "marked_only" else "Все SMMWay"
        text = ("⚙️ <b>Настройки AutoSync</b>\n\n"
                f"⏱ Интервал: {format_interval(cfg.update_interval)}\n"
                f"🔔 Обычные отчёты: {'включены' if cfg.notify_on_complete else 'выключены'}; о проблемах сообщаем всегда.\n"
                f"🔑 API-ключ: <code>{escape(mask_key(cfg.api_key))}</code>\n"
                f"🎯 Порог: {escape(cfg.threshold, 40)} ₽\n\n"
                f"↕️ Направление: <b>{direction}</b>\n"
                "В обе стороны — цена может повышаться и снижаться. Только вверх — снижение запрещено.\n\n"
                f"🎯 Область: <b>{scope}</b>\n"
                "Только smm: on — обновлять явно отмеченные лоты. Все SMMWay — все распознанные "
                "SMMWay-лоты, кроме smm: off.")
        keyboard = K()
        for row in ((("⏱ Интервал", "update_interval"), ("🔔 Уведомления", "notify_on_complete")),
                    (("🔑 API-ключ", "api_key"), ("↕️ Направление", "direction")),
                    (("🎯 Порог", "threshold"), ("🎯 Область лотов", "scope")),
                    (("⬅️ Назад", "home"),)):
            keyboard.row(*(B(label, callback_data=f"{UUID}_{action}") for label, action in row))
        return self.show_menu(chat_id, message_id, text, keyboard)

    def command(self, message):
        if message.from_user.id in self.tg.authorized_users:
            chat_id = message.chat.id
            self.tg.clear_state(chat_id, message.from_user.id)
            self.delete(chat_id, self.menu_messages.pop(chat_id, None))
            self.delete(chat_id, message.message_id)
            self.menu(chat_id)

    def callback(self, call):
        if call.from_user.id not in self.tg.authorized_users:
            self.answer(call, "Нет доступа")
            return
        action = call.data[len(UUID) + 1:]
        chat_id, message_id, uid = call.message.chat.id, call.message.message_id, call.from_user.id
        multiplier_input = self.tg.check_state(chat_id, uid, f"{UUID}_multiplier")
        self.tg.clear_state(chat_id, uid)
        if action in {"home", "settings", "cancel"}:
            self.answer(call)
            if action == "home" or (action == "cancel" and multiplier_input):
                self.menu(chat_id, message_id)
            else:
                self.settings(chat_id, message_id)
            return
        if action in {"sync", "preview", "dead"}:
            if self.runtime.start_run(action, chat_id, message_id):
                self.answer(call, "Обход запущен")
            else:
                self.answer(call, "⏳ Синхронизация уже выполняется.")
            return
        if action in INPUT_PROMPTS:
            self.answer(call)
            keyboard = K().row(B("❌ Отмена", callback_data=f"{UUID}_cancel"))
            prompt = INPUT_PROMPTS[action]
            if action == "update_interval":
                prompt += "\nСейчас: " + format_interval(self.runtime.store.snapshot().update_interval)
            prompt_id = self.show_menu(chat_id, message_id, escape(prompt, 500), keyboard)
            self.tg.set_state(chat_id, prompt_id if prompt_id is not None else message_id, uid, f"{UUID}_{action}", {})
            return
        cfg = self.runtime.store.snapshot()
        changes = {}
        if action in {"enabled", "notify_on_complete"}:
            if action == "enabled" and not cfg.enabled and not cfg.api_key:
                self.answer(call, "Сначала задайте API-ключ")
                return
            changes[action] = not getattr(cfg, action)
        elif action == "direction":
            changes[action] = "up_only" if cfg.direction == "both" else "both"
        elif action == "scope":
            changes[action] = "all_smmway" if cfg.scope == "marked_only" else "marked_only"
        else:
            self.answer(call, "Неизвестное действие")
            return
        try:
            if changes:
                self.runtime.update_config(**changes)
        except OSError:
            logger.error("Не удалось сохранить настройки")
            self.answer(call, "Не удалось сохранить настройки; повторите")
            return
        self.answer(call)
        if action == "enabled":
            self.menu(chat_id, message_id)
        else:
            self.settings(chat_id, message_id)

    def input_field(self, message):
        if message.from_user.id not in self.tg.authorized_users:
            return None
        return next((name for name in INPUT_PROMPTS if self.tg.check_state(
            message.chat.id, message.from_user.id, f"{UUID}_{name}")), None)

    def input_message(self, message):
        name = self.input_field(message)
        if name is None:
            return
        chat_id, uid = message.chat.id, message.from_user.id
        menu_message_id = (self.tg.get_state(chat_id, uid) or {}).get("mid")
        raw = (message.text or "").strip()
        if raw.lower() == "/cancel":
            self.tg.clear_state(chat_id, uid)
            if name == "multiplier":
                self.menu(chat_id, menu_message_id)
            else:
                self.settings(chat_id, menu_message_id)
            return
        if name == "api_key":
            self.delete(chat_id, message.message_id)
        try:
            value = validate_setting(name, raw)
            if name == "api_key":
                if not value:
                    raise ValueError("API-ключ не должен быть пустым.")
                client = SmmWayClient(value)
                try:
                    client.get_services()
                finally:
                    client.close()
                if self.input_field(message) != name:
                    return  # A cancellation/navigation during key validation must prevent saving.
            self.runtime.update_config(**{name: value})
        except (ValueError, SmmWayAPIError) as error:
            self.send(chat_id, "⚠️ " + escape(safe_error(error, raw if name == "api_key" else ""), 300) + "\nПовторите ввод или нажмите «Отмена».")
            return
        except Exception:
            log_exception("Не удалось сохранить ввод", raw if name == "api_key" else "")
            self.send(chat_id, "❌ Не удалось сохранить настройку. Повторите ввод или нажмите «Отмена».")
            return
        self.tg.clear_state(chat_id, uid)
        if name != "api_key":  # Keys are removed immediately, even when validation fails.
            self.delete(chat_id, message.message_id)
        if name == "multiplier":
            self.menu(chat_id, menu_message_id)
        else:
            self.settings(chat_id, menu_message_id)

    def register(self):
        self.cardinal.add_telegram_commands(UUID, [("smmsync", "⚙️ Меню автосинхронизации цен", True)])
        self.tg.msg_handler(self.command, commands=["smmsync"])
        self.tg.cbq_handler(self.callback, func=lambda call: (call.data or "").startswith(f"{UUID}_"))
        self.tg.msg_handler(self.input_message, func=lambda message: message.content_type == "text"
                            and self.input_field(message) is not None
                            and (not (message.text or "").startswith("/") or message.text.strip().lower() == "/cancel"))


def init(cardinal: Cardinal, *args):
    global _runtime
    if _runtime is not None:
        logger.warning("Повторный init AutoSync проигнорирован")
        return
    _runtime = PluginRuntime(cardinal)
    _runtime.ui.register()
    _runtime.start_scheduler()
    logger.info("%s %s инициализирован", NAME, VERSION)


def on_unload():
    if _runtime is not None:
        _runtime.stop()


BIND_TO_PRE_INIT = [init]
BIND_TO_INIT = []
BIND_TO_DELETE = [on_unload]
