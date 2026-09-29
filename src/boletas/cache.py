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

from src.boletas.render import RENDER_VERSION


DEFAULT_DIR = Path(__file__).resolve().parents[2] / ".cache" / "leituras"


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
            os.replace(temporary, self._path(file_hash))
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

    def mark_complete(self, file_hash: str) -> None:
        """Registra que todas as boletas do arquivo foram lidas.

        Só vale quando não sobrou nenhuma falha: um arquivo marcado completo
        é devolvido direto do cache, sem recortar, e uma boleta que faltou
        nunca mais seria tentada.
        """
        with self._lock:
            entry = self._load(file_hash)
            entry["complete"] = True
            self._save(file_hash, entry)

    def complete_entries(self, file_hash: str) -> list[tuple[int, int, dict]] | None:
        """Todas as leituras de um arquivo já lido por inteiro, ou `None`."""
        with self._lock:
            entry = self._load(file_hash)
            if not entry.get("complete"):
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

    def size(self) -> tuple[int, int]:
        """(arquivos, boletas) guardados."""
        arquivos = boletas = 0
        if not self.directory.exists():
            return 0, 0
        for path in self.directory.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if data.get("render_version") != self.render_version:
                continue
            arquivos += 1
            boletas += len(data.get("boletas", {}))
        return arquivos, boletas

    def clear(self) -> None:
        with self._lock:
            self._loaded.clear()
            if self.directory.exists():
                for path in self.directory.glob("*.json"):
                    try:
                        path.unlink()
                    except OSError:
                        pass
