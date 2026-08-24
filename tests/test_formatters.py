from datetime import date

from openpyxl.utils.datetime import to_excel

from src.formatters import (
    format_brl_cents,
    parse_date_value,
    parse_money_cents,
)


def test_dates_brazilian_iso_and_excel_serial():
    expected = date(2026, 7, 2)
    assert parse_date_value("02/07/2026") == expected
    assert parse_date_value("2026-07-02") == expected
    assert parse_date_value(to_excel(expected)) == expected
    assert parse_date_value("02/07/2026") != date(2026, 2, 7)


def test_brazilian_and_excel_numeric_money():
    assert parse_money_cents("R$ 1.234,56") == 123456
    assert parse_money_cents("29,90") == 2990
    assert parse_money_cents(1234.56) == 123456
    assert parse_money_cents(0.005) == 1
    assert format_brl_cents(0) == "0,00"
    assert format_brl_cents(-0) == "0,00"

