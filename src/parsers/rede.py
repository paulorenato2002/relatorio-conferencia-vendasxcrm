from __future__ import annotations

from collections import defaultdict
from typing import Iterable

import openpyxl

from src.config import companies_from_cnpjs, companies_from_text
from src.formatters import (
    normalize_cnpj,
    normalize_identifier,
    normalize_text,
    parse_date_value,
    parse_money_cents,
)
from src.models import RedeData, RedeTransaction
from src.parsers.common import BinarySource, source_buffer, source_name


class RedeParseError(ValueError):
    pass


_ALIASES = {
    "date": {"data_da_venda", "data_venda"},
    "status": {"status_da_venda", "status"},
    "value": {"valor_da_venda_original", "valor_venda_original"},
    "modality": {"modalidade", "modalidade_da_venda"},
    "cnpj": {"cnpj", "cnpj_do_estabelecimento"},
    "establishment": {"nome_do_estabelecimento", "estabelecimento", "nome_fantasia"},
    "cancelled": {"cancelada_pelo_estabelecimento", "cancelado_pelo_estabelecimento"},
    "cancelled_value": {"valor_cancelado", "valor_da_venda_cancelada"},
}

_APPROVED = {"aprovada", "aprovado"}
_PAID = {"pago", "paga"}
_CARD = {"credito", "debito"}
_EXCEPTION_STATUS = {
    "negada",
    "negado",
    "expirada",
    "expirado",
    "devolvida",
    "devolvido",
    "cancelada",
    "cancelado",
}
_YES = {"sim", "s", "yes", "true", "1"}


def _header_map(row: Iterable[object]) -> dict[str, int]:
    normalized = [normalize_identifier(value) for value in row]
    result: dict[str, int] = {}
    for field, aliases in _ALIASES.items():
        for index, value in enumerate(normalized):
            if value in aliases:
                result[field] = index
                break
    return result


def _find_table(workbook: openpyxl.Workbook):
    required = {"date", "status", "value", "modality"}
    for worksheet in workbook.worksheets:
        rows = list(worksheet.iter_rows(values_only=True))
        for index, row in enumerate(rows):
            mapping = _header_map(row)
            if "date" in mapping and required.issubset(mapping):
                return worksheet.title, rows, index, mapping
    raise RedeParseError(
        "Não foi possível localizar a linha que contém 'data da venda' e os "
        "demais cabeçalhos obrigatórios da Rede."
    )


def _cell(row: tuple | list, mapping: dict[str, int], field: str):
    index = mapping.get(field)
    return row[index] if index is not None and index < len(row) else None


def parse_rede(source: BinarySource) -> RedeData:
    file_name = source_name(source, "Rede.xlsx")
    try:
        workbook = openpyxl.load_workbook(
            source_buffer(source), read_only=True, data_only=True
        )
    except Exception as exc:
        raise RedeParseError(f"{file_name}: não foi possível abrir o XLSX da Rede.") from exc

    _, rows, header_index, mapping = _find_table(workbook)
    approved_card: dict = defaultdict(int)
    paid_pix: dict = defaultdict(int)
    transactions: list[RedeTransaction] = []
    exceptions: list[RedeTransaction] = []
    cnpjs: set[str] = set()
    establishment_names: set[str] = set()
    all_dates: set = set()
    errors: list[str] = []

    for excel_row, row in enumerate(rows[header_index + 1 :], header_index + 2):
        if not any(value is not None and value != "" for value in row):
            continue
        try:
            day = parse_date_value(_cell(row, mapping, "date"))
            status = normalize_text(_cell(row, mapping, "status"))
            modality = normalize_text(_cell(row, mapping, "modality"))
            value_cents = parse_money_cents(_cell(row, mapping, "value"))
            if not status or not modality:
                raise ValueError("status ou modalidade vazios")
        except ValueError as exc:
            errors.append(f"linha {excel_row}: {exc}")
            continue

        transaction = RedeTransaction(day, status, modality, value_cents)
        transactions.append(transaction)
        all_dates.add(day)
        if status in _APPROVED and modality in _CARD:
            approved_card[day] += value_cents
        if status in _PAID and modality == "pix":
            paid_pix[day] += value_cents
        if status in _EXCEPTION_STATUS:
            exceptions.append(transaction)

        cancelled = normalize_text(_cell(row, mapping, "cancelled"))
        if cancelled in _YES and status not in {"cancelada", "cancelado"}:
            cancelled_raw = _cell(row, mapping, "cancelled_value")
            cancelled_cents = parse_money_cents(cancelled_raw) if cancelled_raw not in (None, "", "-") else value_cents
            exceptions.append(RedeTransaction(day, "cancelada", modality, cancelled_cents))

        cnpj = normalize_cnpj(_cell(row, mapping, "cnpj"))
        if cnpj:
            cnpjs.add(cnpj)
        establishment = str(_cell(row, mapping, "establishment") or "").strip()
        if establishment:
            establishment_names.add(establishment)

    if errors:
        preview = "; ".join(errors[:5])
        suffix = f" (+{len(errors) - 5} erros)" if len(errors) > 5 else ""
        raise RedeParseError(f"{file_name}: linhas inválidas na Rede: {preview}{suffix}")
    if not transactions:
        raise RedeParseError(f"{file_name}: o relatório da Rede não contém transações válidas.")

    company_candidates = companies_from_cnpjs(cnpjs)
    company_candidates.update(companies_from_text(" ".join(establishment_names)))
    return RedeData(
        file_name=file_name,
        daily_approved_card=dict(approved_card),
        daily_paid_pix=dict(paid_pix),
        all_dates=all_dates,
        transactions=tuple(transactions),
        exception_transactions=tuple(exceptions),
        cnpjs=cnpjs,
        establishment_names=establishment_names,
        company_candidates=company_candidates,
        record_count=len(transactions),
    )

