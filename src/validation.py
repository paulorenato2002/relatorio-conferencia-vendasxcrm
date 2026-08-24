from __future__ import annotations

from datetime import date

from src.config import company_config
from src.formatters import format_date_list
from src.models import CashBatchData, CrmData, RedeData, ValidationResult


def _within(values: set[date], start: date, end: date) -> set[date]:
    return {value for value in values if start <= value <= end}


def _validate_identity(
    result: ValidationResult,
    source_label: str,
    candidates: set[str],
    selected_company: str,
) -> None:
    others = candidates - {selected_company}
    if others:
        result.errors.append(
            f"{source_label} pertence a {', '.join(sorted(others))}, não à empresa "
            f"selecionada ({selected_company})."
        )
    elif len(candidates) > 1:
        result.errors.append(
            f"{source_label} contém identificadores de mais de uma empresa: "
            f"{', '.join(sorted(candidates))}."
        )
    elif not candidates:
        config = company_config(selected_company)
        has_configured_identifier = bool(config.cnpjs or config.branch_codes)
        if has_configured_identifier:
            result.errors.append(
                f"Não foi possível confirmar que {source_label} pertence a {selected_company}."
            )
        else:
            result.warnings.append(
                f"{source_label}: não há CNPJ/filial de {selected_company} configurado; "
                "a validação foi limitada à ausência de identificadores conhecidos da outra empresa."
            )


def validate_inputs(
    company: str,
    start_date: date,
    end_date: date,
    crm: CrmData,
    rede: RedeData,
    cash: CashBatchData,
) -> ValidationResult:
    company_config(company)
    result = ValidationResult(company=company, start_date=start_date, end_date=end_date)
    result.warnings.extend(cash.warnings)
    if start_date > end_date:
        result.errors.append("A data inicial deve ser menor ou igual à data final.")
        return result

    _validate_identity(result, f"o CRM ({crm.file_name})", crm.company_candidates, company)
    _validate_identity(result, f"o relatório Rede ({rede.file_name})", rede.company_candidates, company)

    if cash.companies - {company}:
        result.errors.append(
            "Os PDFs de caixa contêm empresa diferente da selecionada: "
            + ", ".join(sorted(cash.companies - {company}))
            + "."
        )
    if len(cash.companies) > 1:
        result.errors.append("Há mistura de empresas nos PDFs de fechamento de caixa.")

    crm_dates = _within(crm.all_dates, start_date, end_date)
    rede_dates = _within(rede.all_dates, start_date, end_date)
    company_cash = cash.for_company(company)
    cash_dates = _within(set(company_cash), start_date, end_date)
    result.source_dates = {"CRM": crm_dates, "Rede": rede_dates, "Caixa": cash_dates}

    for label, dates in result.source_dates.items():
        if not dates:
            message = f"{label} não possui dados da empresa selecionada dentro do período escolhido."
            if label == "Caixa":
                result.warnings.append(
                    message + " As datas com movimentação ficarão PENDENTES no relatório."
                )
            else:
                result.errors.append(message)

    source_all_dates = {
        "CRM": crm.all_dates,
        "Rede": rede.all_dates,
        "Caixa": set(company_cash),
    }
    for label, all_dates in source_all_dates.items():
        outside = all_dates - _within(all_dates, start_date, end_date)
        if outside:
            result.warnings.append(
                f"{label} possui datas fora do período e elas serão ignoradas: "
                f"{format_date_list(outside)}."
            )

    required_cash_dates = crm_dates | rede_dates
    missing_cash = required_cash_dates - cash_dates
    if missing_cash:
        result.warnings.append(
            "Fechamentos de caixa não enviados para datas com movimentação no CRM ou na Rede: "
            f"{format_date_list(missing_cash)}. Esses dias ficarão PENDENTES; PIX e dinheiro "
            "não serão assumidos como zero."
        )

    comparisons = (("CRM", crm_dates, "Rede", rede_dates), ("CRM", crm_dates, "Caixa", cash_dates), ("Rede", rede_dates, "Caixa", cash_dates))
    seen_messages: set[str] = set()
    for left_label, left_dates, right_label, right_dates in comparisons:
        only_left = left_dates - right_dates
        only_right = right_dates - left_dates
        if only_left:
            message = (
                f"Datas presentes em {left_label} e ausentes em {right_label}: "
                f"{format_date_list(only_left)}."
            )
            if message not in seen_messages:
                result.warnings.append(message)
                seen_messages.add(message)
        if only_right:
            message = (
                f"Datas presentes em {right_label} e ausentes em {left_label}: "
                f"{format_date_list(only_right)}."
            )
            if message not in seen_messages:
                result.warnings.append(message)
                seen_messages.add(message)
    return result
