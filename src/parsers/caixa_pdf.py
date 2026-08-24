from __future__ import annotations

from datetime import date
import re

import pdfplumber

from src.config import companies_from_cash_text, companies_from_text
from src.formatters import normalize_text, parse_date_value, parse_money_cents
from src.models import CashBatchData, CashClosing
from src.parsers.common import BinarySource, source_buffer, source_name


class CashPdfParseError(ValueError):
    pass


_MONEY_RE = re.compile(
    r"(?<!\d)-?(?:R\$\s*)?(?:\d{1,3}(?:\.\d{3})+|\d+),\d{2}(?!\d)",
    re.IGNORECASE,
)
_DATE_RE = r"\d{1,2}/\d{1,2}/\d{4}"


def _extract_text(source: BinarySource, file_name: str) -> str:
    try:
        with pdfplumber.open(source_buffer(source)) as pdf:
            parts = [page.extract_text(x_tolerance=2, y_tolerance=3) or "" for page in pdf.pages]
    except Exception as exc:
        raise CashPdfParseError(f"{file_name}: não foi possível abrir ou ler o PDF.") from exc
    text = "\n".join(parts).strip()
    if not text:
        raise CashPdfParseError(
            f"{file_name}: o PDF não possui texto extraível. Exporte o fechamento como PDF textual."
        )
    return text


def _extract_company(text: str, file_name: str) -> tuple[str, str]:
    matches = companies_from_cash_text(text)
    if len(matches) != 1:
        detail = "nenhuma empresa conhecida" if not matches else "mais de uma empresa"
        raise CashPdfParseError(
            f"{file_name}: não foi possível identificar a empresa do fechamento ({detail})."
        )
    company = next(iter(matches))
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    legal_name = next((line for line in lines if company in companies_from_text(line)), company)
    legal_name = re.sub(r"\s+\d{1,2}:\d{2}:\d{2}\s*$", "", legal_name).strip()
    return company, legal_name


def _extract_date(text: str, file_name: str) -> date:
    period = re.search(
        rf"PER[IÍ]ODO\s+DE\s+({_DATE_RE})\s+AT[EÉ]\s+({_DATE_RE})",
        text,
        re.IGNORECASE,
    )
    if period:
        start = parse_date_value(period.group(1))
        end = parse_date_value(period.group(2))
        if start != end:
            raise CashPdfParseError(
                f"{file_name}: o fechamento cobre {period.group(1)} a {period.group(2)}; "
                "é necessário um fechamento identificável por dia."
            )
        return start
    candidates = re.findall(_DATE_RE, text)
    if not candidates:
        raise CashPdfParseError(f"{file_name}: não foi possível identificar a data do fechamento.")
    return parse_date_value(candidates[0])


def _extract_line_balance(text: str, label: str, file_name: str) -> int:
    for line in text.splitlines():
        normalized = normalize_text(line)
        if re.match(rf"^{re.escape(normalize_text(label))}\b", normalized):
            values = _MONEY_RE.findall(line)
            if values:
                return parse_money_cents(values[-1])
    raise CashPdfParseError(
        f"{file_name}: não foi possível identificar a linha '{label}' e seu Saldo."
    )


def _extract_line_balance_optional(text: str, label: str) -> int | None:
    for line in text.splitlines():
        normalized = normalize_text(line)
        if re.match(rf"^{re.escape(normalize_text(label))}\b", normalized):
            values = _MONEY_RE.findall(line)
            if values:
                return parse_money_cents(values[-1])
    return None


def _sales_balances_before_total(text: str) -> list[int]:
    balances: list[int] = []
    inside_sales = False
    for line in text.splitlines():
        normalized = normalize_text(line)
        if normalized == "vendas":
            inside_sales = True
            continue
        if not inside_sales:
            continue
        if re.match(r"^total\b", normalized):
            break
        if normalized.startswith("forma "):
            continue
        values = _MONEY_RE.findall(line)
        if values:
            balances.append(parse_money_cents(values[-1]))
    return balances


def _extract_store_total(text: str, file_name: str) -> int:
    fallback: list[str] | None = None
    for line in text.splitlines():
        normalized = normalize_text(line)
        values = _MONEY_RE.findall(line)
        if normalized.startswith("total desta loja") and values:
            return parse_money_cents(values[-1])
        if re.match(r"^total\b", normalized) and values:
            fallback = values
    if fallback:
        return parse_money_cents(fallback[-1])
    raise CashPdfParseError(f"{file_name}: não foi possível identificar o Total da loja.")


def parse_cash_pdf(source: BinarySource) -> CashClosing:
    file_name = source_name(source, "fechamento.pdf")
    text = _extract_text(source, file_name)
    company, legal_name = _extract_company(text, file_name)
    day = _extract_date(text, file_name)
    pix_cents = _extract_line_balance(text, "PIX", file_name)
    store_total_cents = _extract_store_total(text, file_name)
    cash_cents = _extract_line_balance_optional(text, "DINHEIRO")
    cash_zero_verified = False
    if cash_cents is None:
        visible_balances = _sales_balances_before_total(text)
        if visible_balances and sum(visible_balances) == store_total_cents:
            cash_cents = 0
            cash_zero_verified = True
        else:
            raise CashPdfParseError(
                f"{file_name}: a linha 'DINHEIRO' não aparece e não foi possível "
                "comprovar saldo zero pela totalização das formas de pagamento."
            )
    branch_match = re.search(r"^\s*Filial\s*:\s*(.+?)\s*$", text, re.IGNORECASE | re.MULTILINE)
    branch = branch_match.group(1).strip() if branch_match else None
    return CashClosing(
        file_name=file_name,
        company=company,
        legal_name=legal_name,
        branch=branch,
        date=day,
        pix_cents=pix_cents,
        cash_cents=cash_cents,
        store_total_cents=store_total_cents,
        cash_zero_verified_from_total=cash_zero_verified,
    )


def parse_cash_pdfs(sources: list[BinarySource] | tuple[BinarySource, ...]) -> CashBatchData:
    closings: dict[tuple[str, date], CashClosing] = {}
    warnings: list[str] = []
    for source in sources:
        closing = parse_cash_pdf(source)
        key = (closing.company, closing.date)
        previous = closings.get(key)
        if previous is None:
            closings[key] = closing
            if closing.cash_zero_verified_from_total:
                warnings.append(
                    f"{closing.file_name}: DINHEIRO não aparece no PDF; saldo R$ 0,00 "
                    "comprovado porque a soma das formas de pagamento confere com o Total da loja."
                )
            continue
        comparable = (
            closing.pix_cents,
            closing.cash_cents,
            closing.store_total_cents,
        )
        previous_comparable = (
            previous.pix_cents,
            previous.cash_cents,
            previous.store_total_cents,
        )
        if comparable == previous_comparable:
            warnings.append(
                f"Duplicidade ignorada: {closing.file_name} repete {previous.file_name} "
                f"para {closing.company} em {closing.date.strftime('%d/%m/%Y')}."
            )
            continue
        raise CashPdfParseError(
            f"Conflito de fechamentos para {closing.company} em "
            f"{closing.date.strftime('%d/%m/%Y')}: {previous.file_name} e "
            f"{closing.file_name} possuem valores diferentes."
        )
    return CashBatchData(closings=closings, warnings=warnings)
