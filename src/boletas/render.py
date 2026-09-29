"""Converte boletas escaneadas em uma imagem PNG por boleta.

Os scans recebidos trazem de duas a três boletas lado a lado na mesma página.
Enviar a página inteira ao modelo de visão desperdiça resolução: o serviço
reduz a imagem até o menor lado ficar em 768px, e o manuscrito é o primeiro a
sofrer. Recortar cada boleta antes mantém o menor lado abaixo desse piso, de
modo que a imagem chega ao modelo sem redução.

O recorte usa projeção de coluna: boletas são separadas por faixas verticais
sem tinta. Não há aprendizado de máquina envolvido.

Tinta sozinha não basta. Boleta escaneada clara — linhas da tabela em cinza
claro, pouco manuscrito — tem colunas inteiras abaixo do limiar de tinta, e o
recorte a tomava por vão: em setembro/2026, 22 páginas saíram com boleta
faltando, partida ao meio ou com três boletas numa imagem só (a boleta 28861 de
01/09 nunca chegou ao modelo). O que distingue a boleta do vão é a tabela: a
coluna que cruza linhas horizontais longas é boleta, por mais clara que seja.
A linha é achada pelo contraste com o papel logo acima e logo abaixo, e não
por um limiar de cinza fixo, porque scan escuro tem fundo cinza uniforme — um
limiar que pegasse a linha clara pegaria o fundo também.

Volume: um mês de uma loja são ~800 boletas em ~330 páginas. As páginas são
geradas uma de cada vez (`iter_boletas`), para que o envio comece na primeira
boleta e a memória fique limitada ao que está em trânsito — antes o lote
inteiro era recortado de antemão, retendo 350 MB e cinco minutos de silêncio
antes da primeira requisição.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from io import BytesIO
from pathlib import Path
from typing import Iterator

import numpy as np
from PIL import Image

from src.parsers.common import BinarySource, source_bytes, source_name


DEFAULT_DPI = 200

# O serviço de visão reduz a imagem até o menor lado ficar neste tamanho antes
# de processá-la. Enviar mais que isso é banda gasta em pixels descartados, e a
# redução do lado deles não é melhor que a nossa.
MAX_SHORT_SIDE = 768

# PNG, que é sem perda. JPEG foi medido e descartado: q90 deixa a imagem 3x
# menor, mas errou a vendedora cursiva em 6 de 21 leituras contra 1 do PNG
# (3 rodadas das 7 boletas de referência; 89,8% contra 93,2% nos campos
# críticos). Como o recorte roda em paralelo com o envio e a rede é o
# gargalo, o tamanho não compensava.
#
# Nível 6 em vez de `optimize=True`: mesmos pixels — é compressão sem perda —,
# 3% maior e 3,7x mais rápido (65 ms contra 241 ms por boleta).
PNG_COMPRESS_LEVEL = 6
MIME_TYPE = "image/png"

# Muda sempre que o recorte muda de um jeito que altere a imagem enviada. Entra
# na chave do cache de leituras: recorte diferente, leitura refeita.
RENDER_VERSION = "2026-09-png6-v2"

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

# Linha horizontal da tabela: pixel mais escuro que o papel `_RULE_OFFSET` px
# acima e abaixo por pelo menos `_RULE_CONTRAST` tons de cinza, em trecho
# contínuo de ao menos `_RULE_MIN_LENGTH` da largura da página. A coluna
# cruzada por `_RULE_MIN_COVERAGE` linhas ou mais não é vão.
_RULE_OFFSET = 4
_RULE_CONTRAST = 18
_RULE_MIN_LENGTH = 0.12
_RULE_MIN_COVERAGE = 3

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


class BoletaRenderError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class BoletaImage:
    """Uma boleta isolada, pronta para ser enviada ao n8n."""

    source_file: str
    file_hash: str
    page: int
    position: int
    data: bytes
    width: int
    height: int
    mime: str = MIME_TYPE

    @property
    def image_id(self) -> str:
        return f"{self.source_file}#p{self.page}b{self.position}"

    @property
    def extension(self) -> str:
        return "png" if self.mime == "image/png" else "jpg"


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_pdf(data: bytes) -> bool:
    return data[:5] == b"%PDF-"


def _check_supported(data: bytes, file_name: str) -> None:
    if not data:
        raise BoletaRenderError(f"{file_name}: arquivo vazio.")
    if not _is_pdf(data) and Path(file_name).suffix.lower() not in _IMAGE_SUFFIXES:
        raise BoletaRenderError(
            f"{file_name}: formato não suportado. Envie PDF escaneado ou imagem "
            "(PNG, JPG, WEBP, BMP, TIFF)."
        )


def count_pages(data: bytes, file_name: str = "boletas.pdf") -> int:
    """Quantidade de páginas, sem renderizar nada. Serve para estimar progresso."""
    _check_supported(data, file_name)
    if not _is_pdf(data):
        return 1
    import pymupdf

    try:
        with pymupdf.open(stream=data, filetype="pdf") as document:
            return document.page_count
    except Exception as exc:
        raise BoletaRenderError(f"{file_name}: não foi possível abrir o PDF.") from exc


def _iter_pages(data: bytes, file_name: str, dpi: int) -> Iterator[Image.Image]:
    """Uma página de cada vez: nunca o documento inteiro em memória."""
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
        with document:
            if document.page_count == 0:
                raise BoletaRenderError(f"{file_name}: o PDF não contém páginas.")
            for page in document:
                pixmap = page.get_pixmap(dpi=dpi, alpha=False)
                # `samples` direto no PIL: passar por PNG custava 346 ms por
                # página contra 74 ms, com resultado idêntico pixel a pixel.
                yield Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
        return

    try:
        yield Image.open(BytesIO(data)).convert("RGB")
    except Exception as exc:
        raise BoletaRenderError(f"{file_name}: não foi possível abrir a imagem.") from exc


def _rule_coverage(band: np.ndarray) -> np.ndarray:
    """Quantas linhas horizontais longas cruzam cada coluna da faixa."""
    width = band.shape[1]
    length = max(2, int(width * _RULE_MIN_LENGTH))
    if length >= width or band.shape[0] <= 2 * _RULE_OFFSET:
        return np.zeros(width, dtype=np.int32)
    gray = band.astype(np.int16)
    k = _RULE_OFFSET
    dark = np.zeros(gray.shape, dtype=bool)
    dark[k:-k] = (gray[k:-k] < gray[: -2 * k] - _RULE_CONTRAST) & (
        gray[k:-k] < gray[2 * k :] - _RULE_CONTRAST
    )
    # Trecho escuro contínuo de `length` pixels começando em cada coluna.
    runs = np.pad(np.cumsum(dark, axis=1, dtype=np.int32), ((0, 0), (1, 0)))
    full = (runs[:, length:] - runs[:, :-length]) == length
    # Coluna x coberta se algum trecho completo começa em [x - length + 1, x].
    starts = np.pad(np.cumsum(full, axis=1, dtype=np.int32), ((0, 0), (1, 0)))
    x = np.arange(width)
    high = np.minimum(x, width - length) + 1
    low = np.maximum(0, x - length + 1)
    return ((starts[:, high] - starts[:, low]) > 0).sum(axis=0)


def find_boleta_columns(image: Image.Image) -> list[tuple[int, int]]:
    """Devolve os intervalos horizontais (início, fim) de cada boleta da página."""
    grayscale = np.asarray(image.convert("L"), dtype=np.uint8)
    height, width = grayscale.shape
    band = grayscale[int(height * _VERTICAL_TRIM) : int(height * (1 - _VERTICAL_TRIM)), :]
    ink_ratio = (band < _INK_THRESHOLD).mean(axis=0)
    empty = (ink_ratio < _EMPTY_MAX) & (_rule_coverage(band) < _RULE_MIN_COVERAGE)

    gutters: list[list[int]] = []
    start: int | None = None
    for index, is_empty in enumerate(empty):
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


def iter_boletas(
    data: bytes,
    file_name: str,
    *,
    dpi: int = DEFAULT_DPI,
    digest: str | None = None,
) -> Iterator[BoletaImage]:
    """Gera as boletas de um arquivo, uma de cada vez, na ordem das páginas.

    Quando a página não se divide em colunas — boleta fotografada sozinha, por
    exemplo — a página inteira é devolvida como uma única boleta.
    """
    _check_supported(data, file_name)
    digest = digest or file_hash(data)
    for page_number, page_image in enumerate(_iter_pages(data, file_name, dpi), start=1):
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
            crop.save(buffer, format="PNG", compress_level=PNG_COMPRESS_LEVEL)
            yield BoletaImage(
                source_file=file_name,
                file_hash=digest,
                page=page_number,
                position=position,
                data=buffer.getvalue(),
                width=crop.width,
                height=crop.height,
            )


def render_boletas(source: BinarySource, dpi: int = DEFAULT_DPI) -> list[BoletaImage]:
    """Recorta todas as boletas de um arquivo de uma vez.

    Para lotes grandes prefira `iter_boletas`: esta função guarda todas as
    imagens em memória antes de devolver.
    """
    file_name = source_name(source, "boletas.pdf")
    data = source_bytes(source)
    results = list(iter_boletas(data, file_name, dpi=dpi))
    if not results:
        raise BoletaRenderError(f"{file_name}: nenhuma boleta foi localizada no arquivo.")
    return results


def render_many(sources: list[BinarySource], dpi: int = DEFAULT_DPI) -> list[BoletaImage]:
    images: list[BoletaImage] = []
    for source in sources:
        images.extend(render_boletas(source, dpi=dpi))
    return images
