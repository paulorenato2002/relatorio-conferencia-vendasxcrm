"""Leitura de um lote de boletas em segundo plano.

Por que em segundo plano: o Streamlit reexecuta o script inteiro a cada clique
na tela, e interromper a execução em curso é como ele faz isso. Uma leitura de
25 minutos dentro do clique do botão era morta pelo primeiro clique em qualquer
outro lugar — e levava junto tudo o que já tinha sido lido. Aqui a leitura
roda numa thread própria, que a tela só consulta.

Como o trabalho flui:

    thread do job ──recorta──► fila limitada ──► N threads de envio ──► n8n
         │                                              │
         └── pula o que já está no cache ◄── grava cada leitura assim que volta

O recorte usa PyMuPDF, que não é seguro entre threads, então fica numa thread
só. Não é gargalo: recortar custa ~80 ms por boleta e ler custa ~2 s.
A fila é limitada para que a memória fique no que está em trânsito, não no
lote inteiro.
"""

from __future__ import annotations

from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
import threading
import time
from typing import Sequence

from src.boletas.cache import LeituraCache
from src.boletas.client import (
    BoletaClientError,
    N8nConfig,
    RawBoleta,
    fetch_boleta,
    new_session,
)
from src.boletas.render import (
    BoletaRenderError,
    count_pages,
    file_hash,
    iter_boletas,
)


STATUS_PREPARING = "preparando"
STATUS_READING = "lendo"
STATUS_DONE = "concluido"
STATUS_CANCELLED = "cancelado"
STATUS_FAILED = "erro"

# Média de boletas por página no lote de setembro/2026 (799 em 332). Só serve
# para estimar o total antes de o recorte chegar ao fim.
_BOLETAS_PER_PAGE_PRIOR = 2.4

# Leituras em andamento, pela impressão digital dos arquivos. Módulo importado
# sobrevive às reexecuções do Streamlit; o `session_state` não sobrevive a aba
# fechada ou página recarregada, e a leitura precisa ser reencontrada nesses
# casos — reenviar os mesmos arquivos acha a leitura em curso.
_REGISTRY: dict[str, "LeituraJob"] = {}
_DEFAULT_CACHE: LeituraCache | None = None


def running_jobs() -> dict[str, "LeituraJob"]:
    return _REGISTRY


def default_cache() -> LeituraCache:
    global _DEFAULT_CACHE
    if _DEFAULT_CACHE is None:
        _DEFAULT_CACHE = LeituraCache()
    return _DEFAULT_CACHE


@dataclass(frozen=True, slots=True)
class UploadedBatchFile:
    name: str
    data: bytes


@dataclass(slots=True)
class JobSnapshot:
    status: str
    total_files: int
    total_pages: int
    pages_done: int
    found: int
    estimated_total: int
    done: int
    from_cache: int
    failed: int
    elapsed: float
    eta: float | None
    warnings: list[str] = field(default_factory=list)
    duplicates: list[tuple[str, str]] = field(default_factory=list)
    error: str | None = None

    @property
    def finished(self) -> bool:
        return self.status in (STATUS_DONE, STATUS_CANCELLED, STATUS_FAILED)

    @property
    def progress(self) -> float:
        if self.status == STATUS_DONE:
            return 1.0
        total = max(self.estimated_total, self.done + self.failed, 1)
        return min(0.99, (self.done + self.failed) / total)


class LeituraJob:
    def __init__(
        self,
        files: Sequence[UploadedBatchFile],
        config: N8nConfig,
        cache: LeituraCache,
        *,
        ignore_cache: bool = False,
    ):
        self.config = config
        self.cache = cache
        self.ignore_cache = ignore_cache
        self._files = list(files)

        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None

        self._status = STATUS_PREPARING
        self._error: str | None = None
        self._warnings: list[str] = []
        self._duplicates: list[tuple[str, str]] = []
        self._results: dict[tuple[int, int, int], RawBoleta] = {}
        self._total_pages = 0
        self._pages_done = 0
        self._found = 0
        self._done = 0
        self._from_cache = 0
        self._failed = 0
        self._started = time.monotonic()
        self._finished_at: float | None = None
        self._network_times: deque[float] = deque(maxlen=40)

        # Por arquivo: leituras pendentes, se o recorte terminou, se algo falhou.
        self._pending: dict[str, int] = {}
        self._rendered: set[str] = set()
        self._file_failed: set[str] = set()

    # --- controle -----------------------------------------------------------

    def start(self) -> "LeituraJob":
        self._thread = threading.Thread(target=self._run, name="leitura-boletas", daemon=True)
        self._thread.start()
        return self

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        if self._thread is None:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    # --- leitura do estado pela tela ---------------------------------------

    def snapshot(self) -> JobSnapshot:
        with self._lock:
            end = self._finished_at or time.monotonic()
            estimated = self._estimated_total_locked()
            remaining = max(0, estimated - self._done - self._failed)
            eta = None
            if len(self._network_times) >= 5 and remaining:
                span = self._network_times[-1] - self._network_times[0]
                if span > 0:
                    rate = (len(self._network_times) - 1) / span
                    eta = remaining / rate
            return JobSnapshot(
                status=self._status,
                total_files=len(self._files),
                total_pages=self._total_pages,
                pages_done=self._pages_done,
                found=self._found,
                estimated_total=estimated,
                done=self._done,
                from_cache=self._from_cache,
                failed=self._failed,
                elapsed=end - self._started,
                eta=eta,
                warnings=list(self._warnings),
                duplicates=list(self._duplicates),
                error=self._error,
            )

    def results(self) -> list[RawBoleta]:
        """Leituras em ordem: arquivo (na ordem do upload), página, posição."""
        with self._lock:
            return [self._results[key] for key in sorted(self._results)]

    def file_names(self) -> tuple[str, ...]:
        duplicados = {name for name, _ in self._duplicates}
        return tuple(f.name for f in self._files if f.name not in duplicados)

    # --- execução -----------------------------------------------------------

    def _estimated_total_locked(self) -> int:
        if self._pages_done:
            per_page = self._found / self._pages_done
        else:
            per_page = _BOLETAS_PER_PAGE_PRIOR
        pages_left = max(0, self._total_pages - self._pages_done)
        return self._found + round(pages_left * per_page)

    def _warn(self, message: str) -> None:
        with self._lock:
            self._warnings.append(message)

    def _record(self, order: int, raw: RawBoleta, *, cached: bool) -> None:
        with self._lock:
            self._results[(order, raw.page, raw.position)] = raw
            self._done += 1
            if cached:
                self._from_cache += 1
            else:
                self._network_times.append(time.monotonic())

    def _maybe_complete(self, digest: str) -> None:
        """Marca o arquivo como lido por inteiro quando nada ficou para trás."""
        with self._lock:
            ready = (
                digest in self._rendered
                and self._pending.get(digest, 0) == 0
                and digest not in self._file_failed
                and not self._cancel.is_set()
            )
        if ready:
            self.cache.mark_complete(digest)

    def _run(self) -> None:
        try:
            self._run_unsafe()
        except Exception as exc:  # pragma: no cover - rede de segurança
            with self._lock:
                self._status = STATUS_FAILED
                self._error = f"{type(exc).__name__}: {exc}"
                self._finished_at = time.monotonic()

    def _run_unsafe(self) -> None:
        # 1. Arquivos repetidos e contagem de páginas — rápido, sem renderizar.
        unique: list[tuple[int, UploadedBatchFile, str]] = []
        seen: dict[str, str] = {}
        for order, item in enumerate(self._files):
            digest = file_hash(item.data)
            if digest in seen:
                with self._lock:
                    self._duplicates.append((item.name, seen[digest]))
                continue
            seen[digest] = item.name
            unique.append((order, item, digest))
        pages_by_file: dict[str, int] = {}
        for _, item, digest in unique:
            try:
                pages_by_file[digest] = count_pages(item.data, item.name)
            except BoletaRenderError as exc:
                self._warn(str(exc))
                pages_by_file[digest] = 0
        with self._lock:
            self._total_pages = sum(pages_by_file.values())
            self._status = STATUS_READING

        # 2. Recorte na thread do job; envio no pool.
        in_flight = threading.BoundedSemaphore(max(2, self.config.concurrency * 2))
        with new_session(self.config.concurrency) as session, ThreadPoolExecutor(
            max_workers=max(1, self.config.concurrency),
            thread_name_prefix="envio-boleta",
        ) as pool:
            for order, item, digest in unique:
                if self._cancel.is_set():
                    break
                if pages_by_file.get(digest, 0) == 0:
                    continue
                if self.ignore_cache:
                    self.cache.forget(digest)
                else:
                    complete = self.cache.complete_entries(digest)
                    if complete is not None:
                        for page, position, payload in complete:
                            self._record(
                                order,
                                RawBoleta(item.name, page, position, payload),
                                cached=True,
                            )
                        with self._lock:
                            self._found += len(complete)
                            self._pages_done += pages_by_file[digest]
                        continue

                self._render_and_submit(order, item, digest, pool, session, in_flight)

            # O `with` do pool espera os envios em curso terminarem.

        with self._lock:
            self._status = STATUS_CANCELLED if self._cancel.is_set() else STATUS_DONE
            self._finished_at = time.monotonic()

    def _render_and_submit(self, order, item, digest, pool, session, in_flight) -> None:
        last_page = 0
        try:
            for image in iter_boletas(item.data, item.name, digest=digest):
                if self._cancel.is_set():
                    break
                if image.page != last_page:
                    with self._lock:
                        self._pages_done += image.page - last_page
                    last_page = image.page
                with self._lock:
                    self._found += 1

                cached = self.cache.get(digest, image.page, image.position)
                if cached is not None:
                    self._record(
                        order, RawBoleta(item.name, image.page, image.position, cached), cached=True
                    )
                    continue

                # Segura o recorte até haver vaga: a memória fica no que está
                # em trânsito, não no lote inteiro.
                while not in_flight.acquire(timeout=0.25):
                    if self._cancel.is_set():
                        return
                with self._lock:
                    self._pending[digest] = self._pending.get(digest, 0) + 1
                future = pool.submit(fetch_boleta, session, self.config, image, self._cancel.is_set)
                future.add_done_callback(
                    lambda f, image=image: self._on_read(f, order, item.name, digest, image, in_flight)
                )
        except BoletaRenderError as exc:
            self._warn(str(exc))
            with self._lock:
                self._file_failed.add(digest)
        finally:
            with self._lock:
                self._rendered.add(digest)
            self._maybe_complete(digest)

    def _on_read(self, future: Future, order, name, digest, image, in_flight) -> None:
        try:
            raw = future.result()
        except BoletaClientError as exc:
            with self._lock:
                self._failed += 1
                self._file_failed.add(digest)
                if not self._cancel.is_set():
                    self._warnings.append(str(exc))
        except Exception as exc:  # pragma: no cover - erro inesperado de rede
            with self._lock:
                self._failed += 1
                self._file_failed.add(digest)
                self._warnings.append(f"{image.image_id}: {type(exc).__name__}: {exc}")
        else:
            self.cache.put(digest, image.page, image.position, raw.payload)
            self._record(order, raw, cached=False)
        finally:
            in_flight.release()
            with self._lock:
                self._pending[digest] = self._pending.get(digest, 1) - 1
            self._maybe_complete(digest)
