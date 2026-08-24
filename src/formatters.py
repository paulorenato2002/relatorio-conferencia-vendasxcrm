from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import math
import re
import unicodedata

from openpyxl.utils.datetime import from_excel


_CENT = Decimal("0.01")


def normalize_text(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", text).strip().lower()


def normalize_identifier(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", normalize_text(value)).strip("_")


def only_digits(value: object) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return str(int(value))
    return re.sub(r"\D", "", str(value))


def normalize_cnpj(value: object) -> str:
    digits = only_digits(value)
    return digits.zfill(14) if digits else ""


def normalize_branch_code(value: object) -> str:
    digits = only_digits(value)
    return digits.zfill(5) if digits and len(digits) <= 5 else digits


def parse_date_value(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if hasattr(value, "to_pydatetime"):
        converted = value.to_pydatetime()
        return converted.date() if isinstance(converted, datetime) else converted
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            converted = from_excel(value)
        except (OverflowError, ValueError, TypeError) as exc:
            raise ValueError(f"Data serial do Excel inválida: {value!r}") from exc
        return converted.date() if isinstance(converted, datetime) else converted

    text = str(value or "").strip()
    if not text:
        raise ValueError("Data vazia")
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return parse_date_value(float(text))

    iso_match = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?:[T\s].*)?$", text)
    if iso_match:
        year, month, day = map(int, iso_match.groups())
        return date(year, month, day)

    br_match = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{2}|\d{4})(?:[T\s].*)?$", text)
    if br_match:
        day, month, year = map(int, br_match.groups())
        if year < 100:
            year += 2000
        return date(year, month, day)
    raise ValueError(f"Formato de data não reconhecido: {value!r}")


def _decimal_from_text(value: str) -> Decimal:
    text = value.strip().replace("R$", "").replace("r$", "").replace(" ", "")
    if text in {"", "-", "--"}:
        return Decimal("0")
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    text = re.sub(r"[^0-9,\.\-+]", "", text)
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        parts = text.split(",")
        text = "".join(parts[:-1]) + "." + parts[-1]
    elif text.count(".") > 1:
        parts = text.split(".")
        text = "".join(parts[:-1]) + "." + parts[-1]
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"Valor monetário inválido: {value!r}") from exc
    return -parsed if negative else parsed


def parse_money_cents(value: object) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        raise ValueError(f"Valor monetário inválido: {value!r}")
    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, int):
        amount = Decimal(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"Valor monetário inválido: {value!r}")
        amount = Decimal(str(value))
    else:
        amount = _decimal_from_text(str(value))
    rounded = amount.quantize(_CENT, rounding=ROUND_HALF_UP)
    return int(rounded * 100)


def cents_to_decimal(cents: int) -> Decimal:
    return (Decimal(cents) / 100).quantize(_CENT)


def format_brl_cents(cents: int) -> str:
    cents = int(cents or 0)
    sign = "-" if cents < 0 else ""
    whole, fraction = divmod(abs(cents), 100)
    grouped = f"{whole:,}".replace(",", ".")
    return f"{sign}{grouped},{fraction:02d}"


def format_brl_currency(cents: int) -> str:
    return f"R$ {format_brl_cents(cents)}"


def format_date_br(value: date) -> str:
    return value.strftime("%d/%m/%Y")


def format_date_list(values: set[date] | list[date] | tuple[date, ...]) -> str:
    return ", ".join(format_date_br(value) for value in sorted(values))

