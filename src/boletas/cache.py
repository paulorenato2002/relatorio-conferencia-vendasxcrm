"""Leituras já feitas, guardadas em disco, uma entrada por boleta.

Um mês de uma loja são ~800 boletas e ~25 minutos de leitura paga. Sem isto a
leitura era tudo-ou-nada: qualquer interrupção — clique na tela, aba fechada,
computador suspenso — jogava fora o que já tinha sido lido e pago, e a próxima
tentativa começava do zero.

Com o cache, cada boleta lida é gravada assim que volta. Recomeçar pula o que
já está aqui; um arquivo lido por inteiro nem é recortado de novo. Dois
arquivos com o mesmo conteúdo (o "21.09" e o "21.09 (1)" que o download do
navegador gera) caem na mesma chave e não são pagos duas vezes.

Chave: hash do conteúdo do arquivo + versão do recorte + página + posição.
O nome do arquivo não entra: renomear não invalida a leitura, e conteúdo
diferente com o mesmo nome não reaproveita leitura errada.

Os arquivos ficam em `.cache/leituras/`, fora do git. Contêm o que o modelo
transcreveu — inclusive nome e telefone de cliente —, então são apagáveis a
qualquer momento sem prejuízo além de ter de ler de novo.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import time

from src.boletas.render import RENDER_VERSION


DEFAULT_DIR = Path(__file__).resolve().parents[2] / ".cache" / "leituras"

_REPLACE_ATTEMPTS = 20
_REPLACE_WAIT = 0.05


def _replace_with_retry(source: str, target: Path) -> None:
    """`os.replace` que aguenta o destino estar aberto por outro processo.

    No Windows a troca falha com "acesso negado" enquanto alguém lê o destino —
    antivírus examinando o arquivo recém-gravado, o indexador de busca, outra
    sessão do app. É passageiro; aconteceu na leitura real de um mês inteiro.
    """
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_WAIT * (attempt + 1))


class LeituraCache:
    def __init__(self, directory: Path | str = DEFAULT_DIR, render_version: str = RENDER_VERSION):
        self.directory = Path(directory)
        self.render_version = render_version
        self._lock = threading.Lock()
        self._loaded: dict[str, dict] = {}

    # --- armazenamento ------------------------------------------------------

    def _path(self, file_hash: str) -> Path:
        return self.directory / f"{file_hash}.json"

    def _empty(self) -> dict:
        return {"render_version": self.render_version, "boletas": {}, "complete": False}

    def _load(self, file_hash: str) -> dict:
        if file_hash in self._loaded:
            return self._loaded[file_hash]
        entry = self._empty()
        try:
            data = json.loads(self._path(file_hash).read_text(encoding="utf-8"))
            # Recorte de outra versão gera imagens diferentes; a leitura antiga
            # não corresponde mais a elas.
            if data.get("render_version") == self.render_version:
                entry = data
        except (OSError, ValueError):
            pass
        self._loaded[file_hash] = entry
        return entry

    def _save(self, file_hash: str, entry: dict) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        # Grava num temporário e troca: uma queda no meio da escrita não deixa
        # JSON pela metade que invalidaria o arquivo inteiro.
        handle, temporary = tempfile.mkstemp(dir=self.directory, suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as output:
                json.dump(entry, output, ensure_ascii=False)
            _replace_with_retry(temporary, self._path(file_hash))
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    @staticmethod
    def _slot(page: int, position: int) -> str:
        return f"p{page}b{position}"

    # --- consulta e gravação ------------------------------------------------

    def get(self, file_hash: str, page: int, position: int) -> dict | None:
        with self._lock:
            return self._load(file_hash)["boletas"].get(self._slot(page, position))

    def put(self, file_hash: str, page: int, position: int, payload: dict) -> None:
        with self._lock:
            entry = self._load(file_hash)
            entry["boletas"][self._slot(page, position)] = payload
            self._save(file_hash, entry)

    def mark_complete(self, file_hash: str, count: int) -> None:
        """Registra que as `count` boletas do arquivo foram lidas.

        Um arquivo marcado completo é devolvido direto do cache, sem recortar —
        uma boleta que faltasse nunca mais seria tentada. Por isso a quantidade
        esperada vai junto e é conferida na volta.
        """
        with self._lock:
            entry = self._load(file_hash)
            entry["complete"] = True
            entry["count"] = count
            self._save(file_hash, entry)

    def complete_entries(self, file_hash: str) -> list[tuple[int, int, dict]] | None:
        """Todas as leituras de um arquivo já lido por inteiro, ou `None`.

        Só confia na marca de completo se o número de leituras guardadas bate
        com o de boletas que o arquivo tinha. Marca sem contagem — gravada antes
        desta conferência existir — ou com buraco faz o arquivo ser recortado de
        novo; o que já está guardado é aproveitado e só o que falta é lido.
        """
        with self._lock:
            entry = self._load(file_hash)
            if not entry.get("complete"):
                return None
            if entry.get("count") != len(entry["boletas"]):
                return None
            result = []
            for slot, payload in entry["boletas"].items():
                page, position = slot[1:].split("b")
                result.append((int(page), int(position), payload))
            return sorted(result, key=lambda item: (item[0], item[1]))

    def forget(self, file_hash: str) -> None:
        with self._lock:
            self._loaded[file_hash] = self._empty()
            try:
                self._path(file_hash).unlink()
            except OSError:
                pass

    def size(self) -> int:
        """Quantos arquivos têm leitura guardada.

        Só lista o diretório. A tela chama isto a cada reexecução, e abrir cada
        JSON seria lento com meses acumulados — e, no Windows, segurar o arquivo
        aberto é justamente o que faz a gravação concorrente falhar.
        """
        if not self.directory.exists():
            return 0
        return sum(1 for _ in self.directory.glob("*.json"))

    def clear(self) -> None:
        with self._lock:
            self._loaded.clear()
            if self.directory.exists():
                for path in self.directory.glob("*.json"):
                    try:
                        path.unlink()
                    except OSError:
                        pass
