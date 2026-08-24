from __future__ import annotations

from dataclasses import dataclass

from src.formatters import normalize_branch_code, normalize_cnpj, normalize_text


@dataclass(frozen=True, slots=True)
class CompanyConfig:
    key: str
    display_name: str
    aliases: tuple[str, ...]
    legal_names: tuple[str, ...] = ()
    cnpjs: tuple[str, ...] = ()
    branch_codes: tuple[str, ...] = ()
    cash_report_names: tuple[str, ...] = ()


# Todos os identificadores empresariais ficam neste arquivo para ajuste futuro.
COMPANIES: dict[str, CompanyConfig] = {
    "Rezende": CompanyConfig(
        key="Rezende",
        display_name="Rezende",
        aliases=("rezende", "morana rezende"),
        cnpjs=("18547721000181",),
        branch_codes=("00353",),
        cash_report_names=("MORANA ASA NORTE BSB",),
    ),
    "L2H": CompanyConfig(
        key="L2H",
        display_name="L2H",
        aliases=(
            "l2h",
            "l2h bijuterias",
            "l2h bijuterias e acessorios femininos ltda",
        ),
        legal_names=("L2H BIJUTERIAS E ACESSORIOS FEMININOS LTDA",),
        # CNPJ e filial não constam nas amostras fornecidas. Inclua-os aqui
        # quando estiverem disponíveis; aliases textuais continuam válidos.
        cnpjs=(),
        branch_codes=(),
        cash_report_names=("MORANA JARDIM BOTANICO SHOPPING",),
    ),
}


def company_config(company: str) -> CompanyConfig:
    try:
        return COMPANIES[company]
    except KeyError as exc:
        raise ValueError(f"Empresa inválida: {company!r}") from exc


def companies_from_text(text: str) -> set[str]:
    normalized = normalize_text(text)
    matches: set[str] = set()
    for key, config in COMPANIES.items():
        candidates = (*config.aliases, *config.legal_names)
        if any(normalize_text(alias) in normalized for alias in candidates if alias):
            matches.add(key)
    return matches


def companies_from_cash_text(text: str) -> set[str]:
    normalized = normalize_text(text)
    matches: set[str] = set()
    for key, config in COMPANIES.items():
        candidates = (*config.aliases, *config.legal_names, *config.cash_report_names)
        if any(normalize_text(alias) in normalized for alias in candidates if alias):
            matches.add(key)
    return matches


def companies_from_branches(branches: set[str]) -> set[str]:
    normalized = {normalize_branch_code(branch) for branch in branches if branch}
    return {
        key
        for key, config in COMPANIES.items()
        if normalized.intersection({normalize_branch_code(code) for code in config.branch_codes})
    }


def companies_from_cnpjs(cnpjs: set[str]) -> set[str]:
    normalized = {normalize_cnpj(cnpj) for cnpj in cnpjs if cnpj}
    return {
        key
        for key, config in COMPANIES.items()
        if normalized.intersection({normalize_cnpj(cnpj) for cnpj in config.cnpjs})
    }
