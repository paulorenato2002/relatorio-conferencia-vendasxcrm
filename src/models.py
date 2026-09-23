from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(slots=True)
class CrmData:
    file_name: str
    daily_by_seller: dict[date, dict[str, int]]
    sellers: tuple[str, ...]
    all_dates: set[date]
    branch_codes: set[str] = field(default_factory=set)
    company_candidates: set[str] = field(default_factory=set)
    record_count: int = 0

    def total_on(self, day: date) -> int:
        return sum(self.daily_by_seller.get(day, {}).values())


@dataclass(frozen=True, slots=True)
class RedeTransaction:
    date: date
    status: str
    modality: str
    value_cents: int


@dataclass(slots=True)
class RedeData:
    file_name: str
    daily_approved_card: dict[date, int]
    daily_paid_pix: dict[date, int]
    all_dates: set[date]
    transactions: tuple[RedeTransaction, ...]
    exception_transactions: tuple[RedeTransaction, ...]
    cnpjs: set[str] = field(default_factory=set)
    establishment_names: set[str] = field(default_factory=set)
    company_candidates: set[str] = field(default_factory=set)
    record_count: int = 0


@dataclass(frozen=True, slots=True)
class CashClosing:
    file_name: str
    company: str
    legal_name: str
    branch: str | None
    date: date
    pix_cents: int
    cash_cents: int
    store_total_cents: int
    absent_payment_methods: tuple[str, ...] = ()


@dataclass(slots=True)
class CashBatchData:
    closings: dict[tuple[str, date], CashClosing]
    warnings: list[str] = field(default_factory=list)

    @property
    def all_dates(self) -> set[date]:
        return {closing.date for closing in self.closings.values()}

    @property
    def companies(self) -> set[str]:
        return {closing.company for closing in self.closings.values()}

    def for_company(self, company: str) -> dict[date, CashClosing]:
        return {
            day: closing
            for (closing_company, day), closing in self.closings.items()
            if closing_company == company
        }


@dataclass(slots=True)
class ValidationResult:
    company: str
    start_date: date
    end_date: date
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source_dates: dict[str, set[date]] = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        return not self.errors


@dataclass(frozen=True, slots=True)
class ReconciliationRow:
    date: date
    sellers_cents: dict[str, int]
    total_crm_cents: int
    pix_cash_cents: int | None
    cash_cents: int | None
    expected_card_cents: int | None
    rede_cents: int
    difference_cents: int | None
    status: str


@dataclass(slots=True)
class ReconciliationReport:
    company: str
    start_date: date
    end_date: date
    sellers: tuple[str, ...]
    rows: list[ReconciliationRow]
    totals: dict[str, int]
    ok_days: int
    divergent_days: int
    pending_days: int

    @property
    def overall_status(self) -> str:
        if self.pending_days:
            return "PENDENTE"
        return "OK" if self.divergent_days == 0 else "DIVERGÊNCIA"


@dataclass(frozen=True, slots=True)
class BoletaItem:
    """Peça lançada na boleta. `codigo` tem 10 dígitos, como impresso na etiqueta."""

    codigo: str
    value_cents: int
    handwritten: bool = False


@dataclass(frozen=True, slots=True)
class Boleta:
    source_file: str
    page: int
    position: int
    numero: str | None
    numero_controle: str | None
    date: date | None
    seller: str | None
    client: str | None
    phone: str | None
    items: tuple[BoletaItem, ...]
    returns: tuple[BoletaItem, ...]
    sub_total_cents: int | None
    discount_cents: int | None
    total_cents: int | None
    piece_count: int | None
    payment_method: str | None
    installments: int | None
    card_brand: str | None
    flags: frozenset[str]
    unreadable_fields: tuple[str, ...]
    checks: tuple[str, ...]

    @property
    def image_id(self) -> str:
        return f"{self.source_file}#p{self.page}b{self.position}"

    @property
    def needs_review(self) -> bool:
        return bool(self.checks or self.unreadable_fields)

    @property
    def items_total_cents(self) -> int:
        return sum(item.value_cents for item in self.items)


@dataclass(slots=True)
class BoletaData:
    file_names: tuple[str, ...]
    boletas: tuple[Boleta, ...]
    warnings: list[str] = field(default_factory=list)

    @property
    def all_dates(self) -> set[date]:
        return {boleta.date for boleta in self.boletas if boleta.date is not None}

    @property
    def sellers(self) -> tuple[str, ...]:
        return tuple(sorted({b.seller for b in self.boletas if b.seller}))

    @property
    def review_queue(self) -> tuple[Boleta, ...]:
        return tuple(boleta for boleta in self.boletas if boleta.needs_review)

    def daily_by_seller(self) -> dict[date, dict[str, int]]:
        totals: dict[date, dict[str, int]] = {}
        for boleta in self.boletas:
            if boleta.date is None or boleta.total_cents is None:
                continue
            seller = boleta.seller or "(SEM VENDEDORA)"
            totals.setdefault(boleta.date, {}).setdefault(seller, 0)
            totals[boleta.date][seller] += boleta.total_cents
        return totals

    def total_on(self, day: date) -> int:
        return sum(self.daily_by_seller().get(day, {}).values())
