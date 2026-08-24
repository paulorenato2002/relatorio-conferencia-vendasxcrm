from __future__ import annotations

from collections import defaultdict
from typing import Iterable

import openpyxl

from src.config import companies_from_branches, companies_from_text
from src.formatters import (
    normalize_branch_code,
    normalize_identifier,
    parse_date_value,
    parse_money_cents,
)
from src.models import CrmData
from src.parsers.common import BinarySource, source_buffer, source_name


class CrmParseError(ValueError):
    pass


_ALIASES = {
    "date": {"data", "data_venda", "data_da_venda"},
    "seller": {"nome_vendedor", "vendedor", "nome_da_vendedora"},
    "commission_base": {
        "valor_base_calculo_comissao",
        "valor_base_de_calculo_de_comissao",
        "base_calculo_comissao",
    },
    "branch": {"filial", "codigo_filial", "cod_filial"},
}


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
    required = {"date", "seller", "commission_base"}
    for worksheet in workbook.worksheets:
        rows = list(worksheet.iter_rows(values_only=True))
        for index, row in enumerate(rows):
            mapping = _header_map(row)
            if required.issubset(mapping):
                return worksheet.title, rows, index, mapping
    raise CrmParseError(
        "Não foi possível localizar os cabeçalhos obrigatórios do CRM: "
        "data, nome_vendedor e valor_base_calculo_comissao."
    )


def parse_crm(source: BinarySource) -> CrmData:
    file_name = source_name(source, "CRM.xlsx")
    try:
        workbook = openpyxl.load_workbook(
            source_buffer(source), read_only=True, data_only=True
        )
    except Exception as exc:
        raise CrmParseError(f"{file_name}: não foi possível abrir o XLSX do CRM.") from exc

    _, rows, header_index, mapping = _find_table(workbook)
    daily: dict = defaultdict(lambda: defaultdict(int))
    branches: set[str] = set()
    hints: list[str] = []
    record_count = 0
    errors: list[str] = []

    for excel_row, row in enumerate(rows[header_index + 1 :], header_index + 2):
        if not any(value is not None and value != "" for value in row):
            continue
        try:
            raw_date = row[mapping["date"]] if mapping["date"] < len(row) else None
            raw_seller = row[mapping["seller"]] if mapping["seller"] < len(row) else None
            raw_value = (
                row[mapping["commission_base"]]
                if mapping["commission_base"] < len(row)
                else None
            )
            if raw_date in (None, "") and raw_seller in (None, ""):
                continue
            day = parse_date_value(raw_date)
            seller = str(raw_seller or "").strip().upper()
            if not seller:
                raise ValueError("nome da vendedora vazio")
            value_cents = parse_money_cents(raw_value)
        except ValueError as exc:
            errors.append(f"linha {excel_row}: {exc}")
            continue
        daily[day][seller] += value_cents
        record_count += 1
        if "branch" in mapping and mapping["branch"] < len(row):
            branch = normalize_branch_code(row[mapping["branch"]])
            if branch:
                branches.add(branch)
        if len(hints) < 100:
            hints.extend(str(value) for value in row if isinstance(value, str))

    if errors:
        preview = "; ".join(errors[:5])
        suffix = f" (+{len(errors) - 5} erros)" if len(errors) > 5 else ""
        raise CrmParseError(f"{file_name}: linhas inválidas no CRM: {preview}{suffix}")
    if not record_count:
        raise CrmParseError(f"{file_name}: o CRM não contém registros de venda válidos.")

    normalized_daily = {day: dict(values) for day, values in daily.items()}
    sellers = tuple(sorted({seller for values in daily.values() for seller in values}))
    company_candidates = companies_from_branches(branches)
    company_candidates.update(companies_from_text(" ".join(hints)))
    return CrmData(
        file_name=file_name,
        daily_by_seller=normalized_daily,
        sellers=sellers,
        all_dates=set(normalized_daily),
        branch_codes=branches,
        company_candidates=company_candidates,
        record_count=record_count,
    )

