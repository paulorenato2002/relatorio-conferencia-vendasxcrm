"""Converte boletas escaneadas em uma imagem PNG por boleta.

Os scans recebidos trazem de duas a três boletas lado a lado na mesma página.
Enviar a página inteira ao modelo de visão desperdiça resolução: o serviço
reduz a imagem até o menor lado ficar em 768px, e o manuscrito é o primeiro a
sofrer. Recortar cada boleta antes mantém o menor lado abaixo desse piso, de
modo que a imagem chega ao modelo sem redução.

O recorte usa projeção de coluna: boletas são separadas por faixas verticais
sem tinta. Não há aprendizado de máquina envolvido.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image

from src.parsers.common import BinarySource, source_bytes, source_name


DEFAULT_DPI = 200

# O serviço de visão reduz a imagem até o menor lado ficar neste tamanho antes
# de processá-la. Enviar mais que isso é banda gasta em pixels descartados, e a
# redução do lado deles não é melhor que a nossa.
MAX_SHORT_SIDE = 768

# Limiar de cinza abaixo do qual o pixel conta como tinta.
_INK_THRESHOLD = 165
# Fração máxima de tinta para a coluna ser considerada vazia.
_EMPTY_MAX = 0.02
# Faixa vazia só separa boletas se ocupar esta fração da largura da página.
_MIN_GUTTER = 0.012
# Faixas vazias separadas por menos que isto são unidas: a lombada escura do
# bloco de boletas aparece no meio do vão e o parte em dois.
_MERGE_GAP = 0.05
# Recorte menor que isto é sujeira de borda, não boleta.
_MIN_SEGMENT = 0.10
# Margem de segurança devolvida a cada lado do recorte.
_PADDING = 8
# Ignora topo e rodapé ao medir tinta (marca d'água do scanner, bordas).
_VERTICAL_TRIM = 0.06

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


class BoletaRenderError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class BoletaImage:
    """Uma boleta isolada, pronta para ser enviada ao n8n."""

    source_file: str
    page: int
    position: int
    png: bytes
    width: int
    height: int

    @property
    def image_id(self) -> str:
        return f"{self.source_file}#p{self.page}b{self.position}"


def _is_pdf(data: bytes) -> bool:
    return data[:5] == b"%PDF-"


def _page_images(data: bytes, file_name: str, dpi: int) -> list[Image.Image]:
    if _is_pdf(data):
        try:
            import pymupdf
        except ImportError as exc:  # pragma: no cover - dependência declarada
            raise BoletaRenderError(
                "PyMuPDF não está instalado. Rode: pip install -r requirements.txt"
            ) from exc
        try:
            document = pymupdf.open(stream=data, filetype="pdf")
        except Exception as exc:
            raise BoletaRenderError(f"{file_name}: não foi possível abrir o PDF.") from exc
        pages = []
        with document:
            for page in document:
                pixmap = page.get_pixmap(dpi=dpi)
                pages.append(Image.open(BytesIO(pixmap.tobytes("png"))).convert("RGB"))
        if not pages:
            raise BoletaRenderError(f"{file_name}: o PDF não contém páginas.")
        return pages

    if Path(file_name).suffix.lower() in _IMAGE_SUFFIXES:
        try:
            return [Image.open(BytesIO(data)).convert("RGB")]
        except Exception as exc:
            raise BoletaRenderError(f"{file_name}: não foi possível abrir a imagem.") from exc

    raise BoletaRenderError(
        f"{file_name}: formato não suportado. Envie PDF escaneado ou imagem "
        "(PNG, JPG, WEBP, BMP, TIFF)."
    )


def find_boleta_columns(image: Image.Image) -> list[tuple[int, int]]:
    """Devolve os intervalos horizontais (início, fim) de cada boleta da página."""
    grayscale = np.asarray(image.convert("L"), dtype=np.uint8)
    height, width = grayscale.shape
    band = grayscale[int(height * _VERTICAL_TRIM) : int(height * (1 - _VERTICAL_TRIM)), :]
    ink_ratio = (band < _INK_THRESHOLD).mean(axis=0)

    gutters: list[list[int]] = []
    start: int | None = None
    for index, is_empty in enumerate(ink_ratio < _EMPTY_MAX):
        if is_empty and start is None:
            start = index
        elif not is_empty and start is not None:
            gutters.append([start, index])
            start = None
    if start is not None:
        gutters.append([start, len(ink_ratio)])

    gutters = [gutter for gutter in gutters if (gutter[1] - gutter[0]) >= width * _MIN_GUTTER]

    merged: list[list[int]] = []
    for gutter in gutters:
        if merged and gutter[0] - merged[-1][1] <= width * _MERGE_GAP:
            merged[-1][1] = gutter[1]
        else:
            merged.append(gutter)

    segments: list[tuple[int, int]] = []
    previous = 0
    for gutter_start, gutter_end in merged:
        if gutter_start - previous >= width * _MIN_SEGMENT:
            segments.append((previous, gutter_start))
        previous = gutter_end
    if width - previous >= width * _MIN_SEGMENT:
        segments.append((previous, width))
    return segments


def _fit_short_side(image: Image.Image, limit: int) -> Image.Image:
    short_side = min(image.width, image.height)
    if limit <= 0 or short_side <= limit:
        return image
    scale = limit / short_side
    return image.resize(
        (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
        Image.LANCZOS,
    )


def render_boletas(source: BinarySource, dpi: int = DEFAULT_DPI) -> list[BoletaImage]:
    """Recorta cada boleta de um scan e devolve uma imagem PNG por boleta.

    Quando a página não se divide em colunas — boleta fotografada sozinha, por
    exemplo — a página inteira é devolvida como uma única boleta.
    """
    file_name = source_name(source, "boletas.pdf")
    data = source_bytes(source)
    if not data:
        raise BoletaRenderError(f"{file_name}: arquivo vazio.")

    results: list[BoletaImage] = []
    for page_number, page_image in enumerate(_page_images(data, file_name, dpi), start=1):
        segments = find_boleta_columns(page_image) or [(0, page_image.width)]
        for position, (left, right) in enumerate(segments, start=1):
            crop = _fit_short_side(
                page_image.crop(
                    (
                        max(0, left - _PADDING),
                        0,
                        min(page_image.width, right + _PADDING),
                        page_image.height,
                    )
                ),
                MAX_SHORT_SIDE,
            )
            buffer = BytesIO()
            crop.save(buffer, format="PNG", optimize=True)
            results.append(
                BoletaImage(
                    source_file=file_name,
                    page=page_number,
                    position=position,
                    png=buffer.getvalue(),
                    width=crop.width,
                    height=crop.height,
                )
            )

    if not results:
        raise BoletaRenderError(f"{file_name}: nenhuma boleta foi localizada no arquivo.")
    return results


def render_many(sources: list[BinarySource], dpi: int = DEFAULT_DPI) -> list[BoletaImage]:
    images: list[BoletaImage] = []
    for source in sources:
        images.extend(render_boletas(source, dpi=dpi))
    return images
