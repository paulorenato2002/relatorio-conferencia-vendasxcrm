"""Leitura das boletas manuscritas via n8n.

Fluxo: scan -> recorte de uma boleta por imagem (`render`) -> webhook do n8n,
que chama o modelo de visão (`client`) -> conferência aritmética e conversão
para os tipos do projeto (`schema`).
"""

from __future__ import annotations

from datetime import date
from typing import Callable

from src.boletas.client import (
    BoletaClientError,
    N8nConfig,
    RawBoleta,
    build_boletas,
    fetch_boletas,
    read_boletas,
)
from src.boletas.render import (
    BoletaImage,
    BoletaRenderError,
    render_boletas,
    render_many,
)
from src.boletas.schema import (
    BoletaSchemaError,
    barcode_to_crm_code,
    flag_date_outliers,
    normalize_barcode,
    normalize_person,
)
from src.models import BoletaData
from src.parsers.common import BinarySource


class BoletaParseError(ValueError):
    pass


def parse_boletas(
    sources: list[BinarySource],
    start: date,
    end: date,
    config: N8nConfig | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> BoletaData:
    """Recorta, lê e confere as boletas dos arquivos enviados."""
    if not sources:
        raise BoletaParseError("Nenhum arquivo de boleta foi enviado.")
    try:
        images = render_many(sources)
    except BoletaRenderError as exc:
        raise BoletaParseError(str(exc)) from exc
    try:
        return read_boletas(images, start, end, config=config, on_progress=on_progress)
    except BoletaClientError as exc:
        raise BoletaParseError(str(exc)) from exc


__all__ = [
    "BoletaClientError",
    "BoletaImage",
    "BoletaParseError",
    "BoletaRenderError",
    "BoletaSchemaError",
    "N8nConfig",
    "RawBoleta",
    "build_boletas",
    "fetch_boletas",
    "barcode_to_crm_code",
    "flag_date_outliers",
    "normalize_barcode",
    "normalize_person",
    "parse_boletas",
    "render_boletas",
    "read_boletas",
    "render_many",
]
