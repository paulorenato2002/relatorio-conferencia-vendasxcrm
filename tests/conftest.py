from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas


ROOT = Path(__file__).resolve().parents[1]


def _provided_sample(filename: str) -> Path:
    path = ROOT / filename
    if not path.exists():
        pytest.skip(
            f"Amostra local não versionada: {filename}",
            allow_module_level=False,
        )
    return path


class NamedBytesIO(BytesIO):
    def __init__(self, data: bytes, name: str):
        super().__init__(data)
        self.name = name


def build_cash_pdf(
    *,
    company="L2H BIJUTERIAS E ACESSORIOS FEMININOS LTDA",
    day="18/07/2026",
    pix="144,80",
    cash="29,90",
    total="4.080,40",
    branch="MORANA JARDIM BOTANICO SHOPPING",
    include_cash=True,
    include_pix=True,
    extra_sales_lines=(),
    name="fechamento.pdf",
) -> NamedBytesIO:
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer)
    y = 800
    lines = [
        company,
        f"{day} RESUMO DE POSIÇÃO DE CAIXA PAG.: 001",
        f"PERÍODO DE {day} ATÉ {day}",
        f"Filial: {branch}",
        "VENDAS",
        "Forma Crédito Débito Acrescimos Saldo",
    ]
    lines.extend(extra_sales_lines)
    if include_cash:
        lines.append(f"DINHEIRO {cash} 0,00 0,00 {cash}")
    if include_pix:
        lines.append(f"PIX {pix} 0,00 0,00 {pix}")
    lines.append(f"TOTAL DESTA LOJA {total} 0,00 0,00 {total}")
    for line in lines:
        pdf.drawString(40, y, line)
        y -= 22
    pdf.save()
    return NamedBytesIO(buffer.getvalue(), name)


@pytest.fixture(scope="session")
def crm_path() -> Path:
    return _provided_sample("Consulta Vendas por Vendedor (55).xlsx")


@pytest.fixture(scope="session")
def rede_path() -> Path:
    return _provided_sample("Rede_Rel_Vendas_02_07_2026-19_07_2026.xlsx")


@pytest.fixture(scope="session")
def caixa_path() -> Path:
    return _provided_sample("CAIXA 18 JULHO.pdf")
