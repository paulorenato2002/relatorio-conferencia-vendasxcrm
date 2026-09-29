"""Leitura em lote: retomada, duplicados, rate limit, cancelamento.

Roda contra um n8n falso local e PDFs sintéticos, sem chamar a OpenAI. O que
se protege aqui é o que quebrou com 27 dias de boletas: leitura tudo-ou-nada,
arquivo repetido pago duas vezes e rate limit virando boleta perdida.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import re
import threading
import time

from PIL import Image, ImageDraw
import pytest

from src.boletas.cache import LeituraCache
from src.boletas.client import N8nConfig
from src.boletas.job import (
    STATUS_CANCELLED,
    STATUS_DONE,
    LeituraJob,
    UploadedBatchFile,
)


# --- PDFs sintéticos --------------------------------------------------------


def _pagina(boletas: int, semente: int = 0) -> Image.Image:
    """Página com `boletas` colunas separadas por faixa branca, como o scan."""
    largura, altura = 1653, 2339
    img = Image.new("RGB", (largura, altura), "white")
    draw = ImageDraw.Draw(img)
    margem, vao = 60, 70
    coluna = (largura - 2 * margem - (boletas - 1) * vao) // boletas
    for i in range(boletas):
        x0 = margem + i * (coluna + vao)
        x1 = x0 + coluna
        draw.rectangle([x0, 150, x1, altura - 150], outline="black", width=6)
        for y in range(200, altura - 200, 45):
            draw.line([x0, y, x1, y], fill="black", width=3)
        # marca própria por boleta para que as imagens não sejam idênticas
        draw.text((x0 + 20, 170), f"{semente}-{i}", fill="black")
    return img


def pdf(paginas: list[int], semente: int = 0) -> bytes:
    imagens = [_pagina(n, semente * 100 + i) for i, n in enumerate(paginas)]
    buf = BytesIO()
    # Data fixa: sem ela o Pillow carimba o horário atual e duas chamadas
    # geram arquivos diferentes — o cache, com razão, não reaproveitaria.
    data_fixa = time.gmtime(1790078400)  # 22/09/2026 12:00 UTC
    imagens[0].save(buf, "PDF", save_all=True, append_images=imagens[1:], resolution=200,
                    creationDate=data_fixa, modDate=data_fixa)
    return buf.getvalue()


# --- n8n falso --------------------------------------------------------------


class FakeN8n:
    def __init__(self):
        self.requests: list[str] = []
        self.rate_limit_first: dict[str, int] = {}   # image_id -> quantas vezes negar
        self.fail_always: set[str] = set()
        self.delay = 0.0
        self.concurrent = 0
        self.max_concurrent = 0
        self._lock = threading.Lock()

        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                image_id = re.search(rb'name="image_id"\r\n\r\n([^\r]*)', body).group(1).decode()
                with fake._lock:
                    fake.requests.append(image_id)
                    fake.concurrent += 1
                    fake.max_concurrent = max(fake.max_concurrent, fake.concurrent)
                    negar = fake.rate_limit_first.get(image_id, 0)
                    if negar:
                        fake.rate_limit_first[image_id] = negar - 1
                try:
                    if fake.delay:
                        time.sleep(fake.delay)
                    if negar:
                        resposta = [{"image_id": image_id, "error":
                                     "Falha na leitura: Rate limit reached for gpt-4o. "
                                     "Please try again in 50ms."}]
                    elif image_id in fake.fail_always:
                        resposta = [{"image_id": image_id,
                                     "error": "Resposta do modelo não é JSON: Unexpected token"}]
                    else:
                        resposta = [{"image_id": image_id, "boleta": {
                            "numero": image_id, "data": "22/09", "vendedora": "Carla",
                            "itens": [], "total": "10.00"}}]
                    dados = json.dumps(resposta).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(dados)))
                    self.end_headers()
                    self.wfile.write(dados)
                finally:
                    with fake._lock:
                        fake.concurrent -= 1

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/webhook/boletas"

    def config(self, concurrency=4, attempts=3) -> N8nConfig:
        return N8nConfig(webhook_url=self.url, token="t", concurrency=concurrency,
                         attempts=attempts, timeout=10)

    def close(self):
        self.server.shutdown()


@pytest.fixture
def n8n():
    fake = FakeN8n()
    yield fake
    fake.close()


@pytest.fixture
def cache(tmp_path):
    return LeituraCache(tmp_path / "leituras")


def rodar(files, config, cache, **kw) -> LeituraJob:
    job = LeituraJob(files, config, cache, **kw).start()
    assert job.wait(timeout=60), "a leitura não terminou"
    return job


# --- leitura ----------------------------------------------------------------


def test_le_todas_as_boletas_na_ordem(n8n, cache):
    files = [UploadedBatchFile("a.pdf", pdf([3, 2], 1)), UploadedBatchFile("b.pdf", pdf([1], 2))]
    job = rodar(files, n8n.config(), cache)
    snap = job.snapshot()
    assert snap.status == STATUS_DONE
    assert snap.done == 6 and snap.failed == 0
    ordem = [(r.source_file, r.page, r.position) for r in job.results()]
    assert ordem == [("a.pdf", 1, 1), ("a.pdf", 1, 2), ("a.pdf", 1, 3),
                     ("a.pdf", 2, 1), ("a.pdf", 2, 2), ("b.pdf", 1, 1)]


def test_concorrencia_respeita_o_configurado(n8n, cache):
    n8n.delay = 0.15
    rodar([UploadedBatchFile("a.pdf", pdf([3, 3, 3], 1))], n8n.config(concurrency=2), cache)
    assert n8n.max_concurrent <= 2


# --- retomada ---------------------------------------------------------------


def test_segunda_leitura_sai_toda_do_cache_sem_chamar_o_n8n(n8n, cache):
    files = [UploadedBatchFile("a.pdf", pdf([3, 2], 1))]
    rodar(files, n8n.config(), cache)
    chamadas = len(n8n.requests)

    job = rodar(files, n8n.config(), cache)
    snap = job.snapshot()
    assert len(n8n.requests) == chamadas, "releu o que já estava no cache"
    assert snap.done == 5 and snap.from_cache == 5


def test_cache_nao_depende_do_nome_do_arquivo(n8n, cache):
    conteudo = pdf([2], 1)
    rodar([UploadedBatchFile("original.pdf", conteudo)], n8n.config(), cache)
    chamadas = len(n8n.requests)
    job = rodar([UploadedBatchFile("renomeado.pdf", conteudo)], n8n.config(), cache)
    assert len(n8n.requests) == chamadas
    assert {r.source_file for r in job.results()} == {"renomeado.pdf"}


def test_falha_definitiva_e_retentada_na_proxima_rodada_so_ela(n8n, cache):
    files = [UploadedBatchFile("a.pdf", pdf([3], 1))]
    n8n.fail_always = {"a.pdf#p1b2"}
    primeira = rodar(files, n8n.config(), cache).snapshot()
    assert primeira.done == 2 and primeira.failed == 1

    n8n.fail_always = set()
    n8n.requests.clear()
    segunda = rodar(files, n8n.config(), cache).snapshot()
    assert n8n.requests == ["a.pdf#p1b2"], "deveria reenviar só a boleta que falhou"
    assert segunda.done == 3 and segunda.failed == 0


def test_ignorar_cache_rele_tudo(n8n, cache):
    files = [UploadedBatchFile("a.pdf", pdf([2], 1))]
    rodar(files, n8n.config(), cache)
    n8n.requests.clear()
    rodar(files, n8n.config(), cache, ignore_cache=True)
    assert len(n8n.requests) == 2


# --- duplicados -------------------------------------------------------------


def test_arquivo_repetido_nao_e_lido_nem_contado_duas_vezes(n8n, cache):
    # Caso real: "Boletas Daniely 21.09.pdf" e "Boletas Daniely 21.09 (1).pdf",
    # byte a byte iguais. Lidos os dois, as peças contariam em dobro no cruzamento.
    conteudo = pdf([3], 1)
    files = [UploadedBatchFile("21.09.pdf", conteudo), UploadedBatchFile("21.09 (1).pdf", conteudo)]
    job = rodar(files, n8n.config(), cache)
    snap = job.snapshot()
    assert len(n8n.requests) == 3
    assert snap.done == 3
    assert snap.duplicates == [("21.09 (1).pdf", "21.09.pdf")]
    assert job.file_names() == ("21.09.pdf",)


# --- rate limit -------------------------------------------------------------


def test_rate_limit_e_retentado_ate_passar(n8n, cache):
    # Antes, o rate limit da OpenAI voltava do n8n como erro comum e a boleta
    # era dada como perdida na primeira tentativa.
    n8n.rate_limit_first = {"a.pdf#p1b1": 3}
    job = rodar([UploadedBatchFile("a.pdf", pdf([2], 1))], n8n.config(attempts=2), cache)
    snap = job.snapshot()
    assert snap.failed == 0 and snap.done == 2
    assert n8n.requests.count("a.pdf#p1b1") == 4


# --- cancelamento -----------------------------------------------------------


def test_cancelar_guarda_o_que_ja_foi_lido_e_retomar_termina(n8n, cache):
    n8n.delay = 0.2
    files = [UploadedBatchFile("a.pdf", pdf([3, 3, 3, 3], 1))]
    job = LeituraJob(files, n8n.config(concurrency=1), cache).start()
    while job.snapshot().done < 2:
        time.sleep(0.05)
    job.cancel()
    assert job.wait(timeout=30)
    parcial = job.snapshot()
    assert parcial.status == STATUS_CANCELLED
    assert 2 <= parcial.done < 12

    n8n.delay = 0
    n8n.requests.clear()
    final = rodar(files, n8n.config(), cache).snapshot()
    assert final.done == 12
    assert len(n8n.requests) == 12 - parcial.done, "releu o que já tinha sido lido"


# --- progresso --------------------------------------------------------------


def test_progresso_termina_em_100_por_cento(n8n, cache):
    job = rodar([UploadedBatchFile("a.pdf", pdf([3, 1], 1))], n8n.config(), cache)
    snap = job.snapshot()
    assert snap.progress == 1.0
    assert snap.total_pages == 2 and snap.pages_done == 2
    assert snap.estimated_total == 4
