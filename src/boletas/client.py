"""Cliente do webhook n8n que lê as boletas.

Uma requisição por boleta. É mais lento em rede que mandar tudo de uma vez,
mas dá retentativa por boleta, mantém o payload pequeno (o n8n Cloud limita o
corpo da requisição) e faz o progresso aparecer na tela conforme cada boleta
volta. Uma boleta que falha não derruba o lote.

A chave do modelo de visão fica na credencial do n8n. Este módulo conhece
apenas a URL do webhook e o token compartilhado.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
import json
import os
from pathlib import Path
import time
from typing import Callable, Iterable

import requests

from src.boletas.render import BoletaImage
from src.boletas.schema import BoletaSchemaError, build_boleta, flag_date_outliers
from src.models import Boleta, BoletaData


class BoletaClientError(RuntimeError):
    pass


DEFAULT_TIMEOUT = 120.0
DEFAULT_ATTEMPTS = 3
DEFAULT_CONCURRENCY = 4
DEFAULT_HEADER = "X-Boletas-Token"

_RETRY_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

_DOTENV = Path(__file__).resolve().parents[2] / ".env"


def _dotenv_values() -> dict[str, str]:
    """Lê o `.env` da raiz do projeto. Ausente ou ilegível vira dicionário vazio."""
    try:
        text = _DOTENV.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


@dataclass(frozen=True, slots=True)
class N8nConfig:
    webhook_url: str
    token: str | None = None
    header_name: str = DEFAULT_HEADER
    timeout: float = DEFAULT_TIMEOUT
    attempts: int = DEFAULT_ATTEMPTS
    concurrency: int = DEFAULT_CONCURRENCY

    @classmethod
    def from_env(cls, overrides: dict[str, str] | None = None) -> "N8nConfig":
        """Configuração em três camadas: `.env` < variáveis de ambiente < `overrides`.

        `overrides` é por onde o Streamlit passa o `st.secrets`, que tem a
        última palavra.
        """
        values = _dotenv_values()
        values.update(os.environ)
        values.update({k: str(v) for k, v in (overrides or {}).items() if v is not None})
        url = (values.get("N8N_BOLETAS_WEBHOOK_URL") or "").strip()
        if not url:
            raise BoletaClientError(
                "N8N_BOLETAS_WEBHOOK_URL não configurada. Copie o .env.example "
                "para .env e informe a URL de produção do webhook do n8n."
            )
        return cls(
            webhook_url=url,
            token=(values.get("N8N_BOLETAS_TOKEN") or "").strip() or None,
            header_name=(values.get("N8N_BOLETAS_HEADER") or DEFAULT_HEADER).strip(),
            timeout=float(values.get("N8N_BOLETAS_TIMEOUT") or DEFAULT_TIMEOUT),
            attempts=int(values.get("N8N_BOLETAS_ATTEMPTS") or DEFAULT_ATTEMPTS),
            concurrency=int(values.get("N8N_BOLETAS_CONCURRENCY") or DEFAULT_CONCURRENCY),
        )


def _headers(config: N8nConfig) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if config.token:
        headers[config.header_name] = config.token
    return headers


def _post_once(
    session: requests.Session, config: N8nConfig, image: BoletaImage
) -> dict:
    response = session.post(
        config.webhook_url,
        headers=_headers(config),
        files={"boleta": (f"{image.page}-{image.position}.png", image.png, "image/png")},
        data={
            "source_file": image.source_file,
            "page": str(image.page),
            "position": str(image.position),
            "image_id": image.image_id,
        },
        timeout=config.timeout,
    )
    if response.status_code in _RETRY_STATUSES:
        raise requests.HTTPError(
            f"HTTP {response.status_code}", response=response
        )
    if response.status_code >= 400:
        detail = response.text.strip()[:300]
        raise BoletaClientError(
            f"O n8n respondeu HTTP {response.status_code} para {image.image_id}: {detail}"
        )
    try:
        return response.json()
    except json.JSONDecodeError as exc:
        raise BoletaClientError(
            f"O n8n devolveu uma resposta que não é JSON para {image.image_id}: "
            f"{response.text.strip()[:300]}"
        ) from exc


def _extract(payload: object) -> dict:
    """Aceita `{...}`, `{"boleta": {...}}` e a lista que o n8n devolve por padrão."""
    if isinstance(payload, list):
        if not payload:
            raise BoletaSchemaError("O n8n devolveu uma lista vazia.")
        payload = payload[0]
    if not isinstance(payload, dict):
        raise BoletaSchemaError("O n8n não devolveu um objeto JSON.")
    if "error" in payload and payload["error"]:
        raise BoletaSchemaError(str(payload["error"]))
    inner = payload.get("boleta")
    return inner if isinstance(inner, dict) else payload


@dataclass(frozen=True, slots=True)
class RawBoleta:
    """Transcrição crua, antes de virar `Boleta`.

    A leitura é a etapa cara — é ela que chama o modelo. Guardar o cru permite
    reconstruir as boletas quando só o período muda na tela, sem pagar de novo
    pela mesma imagem.
    """

    source_file: str
    page: int
    position: int
    payload: dict


def fetch_boleta(
    session: requests.Session, config: N8nConfig, image: BoletaImage
) -> RawBoleta:
    last_error: Exception | None = None
    for attempt in range(1, config.attempts + 1):
        try:
            response = _post_once(session, config, image)
            return RawBoleta(
                source_file=image.source_file,
                page=image.page,
                position=image.position,
                payload=_extract(response),
            )
        except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as exc:
            last_error = exc
            if attempt < config.attempts:
                time.sleep(min(2 ** (attempt - 1), 8))
        except BoletaSchemaError as exc:
            raise BoletaClientError(f"{image.image_id}: {exc}") from exc
    raise BoletaClientError(
        f"{image.image_id}: o n8n não respondeu após {config.attempts} tentativas "
        f"({last_error})."
    )


def fetch_boletas(
    images: Iterable[BoletaImage],
    config: N8nConfig | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[list[RawBoleta], list[str]]:
    """Envia cada boleta ao n8n. Falha individual vira aviso, não exceção.

    O operador enxerga quais boletas não voltaram e reenvia só elas, em vez de
    perder o lote inteiro por causa de uma imagem.
    """
    config = config or N8nConfig.from_env()
    images = list(images)
    if not images:
        raise BoletaClientError("Nenhuma boleta foi recortada dos arquivos enviados.")

    raw: list[RawBoleta] = []
    warnings: list[str] = []
    done = 0

    with requests.Session() as session:
        with ThreadPoolExecutor(max_workers=max(1, config.concurrency)) as pool:
            futures = [
                pool.submit(fetch_boleta, session, config, image) for image in images
            ]
            for future in as_completed(futures):
                try:
                    raw.append(future.result())
                except BoletaClientError as exc:
                    warnings.append(str(exc))
                done += 1
                if on_progress:
                    on_progress(done, len(images))

    raw.sort(key=lambda item: (item.source_file, item.page, item.position))
    return raw, warnings


def build_boletas(
    raw: Iterable[RawBoleta],
    start: date,
    end: date,
    file_names: Iterable[str] = (),
    warnings: Iterable[str] = (),
) -> BoletaData:
    """Converte as transcrições cruas em `BoletaData`. Não usa rede."""
    raw = list(raw)
    boletas: list[Boleta] = []
    problems = list(warnings)
    for item in raw:
        try:
            boletas.append(
                build_boleta(
                    item.payload,
                    source_file=item.source_file,
                    page=item.page,
                    position=item.position,
                    start=start,
                    end=end,
                )
            )
        except BoletaSchemaError as exc:
            problems.append(
                f"{item.source_file}#p{item.page}b{item.position}: {exc}"
            )
    names = tuple(file_names) or tuple(dict.fromkeys(item.source_file for item in raw))
    return BoletaData(
        file_names=names,
        boletas=tuple(flag_date_outliers(boletas)),
        warnings=problems,
    )


def read_boletas(
    images: Iterable[BoletaImage],
    start: date,
    end: date,
    config: N8nConfig | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> BoletaData:
    """Atalho: lê e monta numa passada só."""
    images = list(images)
    raw, warnings = fetch_boletas(images, config=config, on_progress=on_progress)
    return build_boletas(
        raw,
        start,
        end,
        file_names=tuple(dict.fromkeys(image.source_file for image in images)),
        warnings=warnings,
    )
