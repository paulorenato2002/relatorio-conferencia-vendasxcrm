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
import random
import re
import time
from typing import Callable, Iterable

import requests
import requests.adapters

from src.boletas.render import BoletaImage
from src.boletas.schema import BoletaSchemaError, build_boleta, flag_date_outliers
from src.models import Boleta, BoletaData


class BoletaClientError(RuntimeError):
    pass


DEFAULT_TIMEOUT = 120.0
DEFAULT_ATTEMPTS = 3
DEFAULT_CONCURRENCY = 8
DEFAULT_HEADER = "X-Boletas-Token"

_RETRY_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
# Rate limit é o limite de ritmo da conta da OpenAI, esperado num lote grande.
_RATE_LIMIT_ATTEMPTS = 8
_MAX_BACKOFF = 60.0

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


class RetryableError(Exception):
    """Falha passageira: vale esperar e mandar de novo.

    `retry_after` vem do cabeçalho `Retry-After` ou do texto da OpenAI ("Please
    try again in 1.2s"), quando há; senão a espera é a exponencial padrão.
    """

    def __init__(self, message: str, retry_after: float | None = None, rate_limited: bool = False):
        super().__init__(message)
        self.retry_after = retry_after
        self.rate_limited = rate_limited


# O texto de erro repassado pelo n8n é a única pista de que a falha foi da
# OpenAI e passageira. Com o workflow antigo, que não devolve o status HTTP de
# origem, é por aqui que um rate limit deixa de virar leitura perdida.
_TRANSIENT_PATTERNS = re.compile(
    r"rate.?limit|too many requests|\b429\b|\b50[0234]\b|timed? ?out|timeout|"
    r"overloaded|server.?error|server had an error|temporarily|try again|"
    r"ECONNRESET|socket hang up",
    re.IGNORECASE,
)
_RATE_LIMIT_PATTERNS = re.compile(r"rate.?limit|too many requests|\b429\b", re.IGNORECASE)
_TRY_AGAIN_IN = re.compile(r"try again in\s+([\d.]+)\s*(ms|s)\b", re.IGNORECASE)


def _retry_after_from_text(text: str) -> float | None:
    match = _TRY_AGAIN_IN.search(text or "")
    if not match:
        return None
    value = float(match.group(1))
    return value / 1000 if match.group(2).lower() == "ms" else value


def _retry_after_from_header(response: requests.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def _post_once(
    session: requests.Session, config: N8nConfig, image: BoletaImage
) -> dict:
    response = session.post(
        config.webhook_url,
        headers=_headers(config),
        files={
            "boleta": (
                f"{image.page}-{image.position}.{image.extension}",
                image.data,
                image.mime,
            )
        },
        data={
            "source_file": image.source_file,
            "page": str(image.page),
            "position": str(image.position),
            "image_id": image.image_id,
        },
        timeout=config.timeout,
    )
    if response.status_code in _RETRY_STATUSES:
        raise RetryableError(
            f"n8n respondeu HTTP {response.status_code}",
            retry_after=_retry_after_from_header(response),
            rate_limited=response.status_code == 429,
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
    """Aceita `{...}`, `{"boleta": {...}}` e a lista que o n8n devolve por padrão.

    Um `{error}` vindo do workflow é classificado: falha passageira da OpenAI
    (rate limit, sobrecarga, timeout) volta a ser tentada; o resto é definitivo.
    """
    if isinstance(payload, list):
        if not payload:
            raise BoletaSchemaError("O n8n devolveu uma lista vazia.")
        payload = payload[0]
    if not isinstance(payload, dict):
        raise BoletaSchemaError("O n8n não devolveu um objeto JSON.")
    if "error" in payload and payload["error"]:
        message = str(payload["error"])
        upstream = payload.get("upstream_status")
        try:
            upstream = int(upstream) if upstream is not None else None
        except (TypeError, ValueError):
            upstream = None
        transient = (upstream in _RETRY_STATUSES) or bool(_TRANSIENT_PATTERNS.search(message))
        if transient:
            raise RetryableError(
                message,
                retry_after=_retry_after_from_text(message),
                rate_limited=upstream == 429 or bool(_RATE_LIMIT_PATTERNS.search(message)),
            )
        raise BoletaSchemaError(message)
    inner = payload.get("boleta")
    return inner if isinstance(inner, dict) else payload


def _backoff(attempt: int, retry_after: float | None, rate_limited: bool) -> float:
    """Espera antes da próxima tentativa, com jitter para não sincronizar threads.

    Rate limit espera mais: é a OpenAI pedindo para diminuir o ritmo, e voltar
    em um segundo com quatro threads só renova o bloqueio.
    """
    base = 2.0 if rate_limited else 1.0
    wait = min(_MAX_BACKOFF, base * 2 ** (attempt - 1))
    if retry_after is not None:
        wait = max(wait, retry_after)
    return min(_MAX_BACKOFF, wait * random.uniform(0.8, 1.3))


def new_session(concurrency: int) -> requests.Session:
    """Sessão com pool do tamanho da concorrência.

    O padrão do `requests` guarda 10 conexões por host; acima disso cada
    requisição excedente abre e descarta conexão, e o log enche de aviso.
    """
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=1, pool_maxsize=max(10, concurrency * 2)
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


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
    session: requests.Session,
    config: N8nConfig,
    image: BoletaImage,
    should_stop: Callable[[], bool] | None = None,
) -> RawBoleta:
    """Lê uma boleta, tentando de novo enquanto a falha for passageira.

    Rate limit ganha mais tentativas que as outras falhas: num lote de 800 ele
    é esperado, não excepcional, e desistir cedo transforma o limite de ritmo da
    OpenAI em boleta perdida.
    """
    last_error: Exception | None = None
    limit = config.attempts
    attempt = 0
    while attempt < limit:
        attempt += 1
        try:
            response = _post_once(session, config, image)
            return RawBoleta(
                source_file=image.source_file,
                page=image.page,
                position=image.position,
                payload=_extract(response),
            )
        except RetryableError as exc:
            last_error = exc
            if exc.rate_limited:
                limit = max(limit, _RATE_LIMIT_ATTEMPTS)
            wait = _backoff(attempt, exc.retry_after, exc.rate_limited)
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = exc
            wait = _backoff(attempt, None, False)
        except BoletaSchemaError as exc:
            raise BoletaClientError(f"{image.image_id}: {exc}") from exc
        if attempt >= limit:
            break
        # Dorme em fatias para que um cancelamento não espere o backoff inteiro.
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if should_stop and should_stop():
                raise BoletaClientError(f"{image.image_id}: leitura cancelada.")
            time.sleep(min(0.25, deadline - time.monotonic()))
    raise BoletaClientError(
        f"{image.image_id}: o n8n não respondeu após {attempt} tentativa(s) "
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

    with new_session(config.concurrency) as session:
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
