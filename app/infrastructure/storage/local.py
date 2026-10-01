from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import UploadFile

from app.core.errors import bad_request, payload_too_large


class LocalObjectStorage:
    def __init__(self, root: Path, *, chunk_size: int, max_upload_bytes: int) -> None:
        self.root = root.resolve()
        self.chunk_size = chunk_size
        self.max_upload_bytes = max_upload_bytes

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for prefix in ("quarantine", "accepted", "rejected"):
            (self.root / prefix).mkdir(parents=True, exist_ok=True)

    def resolve(self, key: str) -> Path:
        normalized = PurePosixPath(key)
        if (
            normalized.is_absolute()
            or not normalized.parts
            or any(part in {"", ".", ".."} for part in normalized.parts)
        ):
            raise bad_request("INVALID_STORAGE_KEY", "A chave de armazenamento é inválida.")
        if "\\" in key:
            raise bad_request("INVALID_STORAGE_KEY", "A chave de armazenamento deve usar separadores POSIX.")
        candidate = self.root.joinpath(*normalized.parts).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise bad_request("INVALID_STORAGE_KEY", "A chave aponta para fora do armazenamento configurado.")
        return candidate

    async def put_upload(self, key: str, upload: UploadFile) -> tuple[int, str]:
        target = self.resolve(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        total = 0
        try:
            with target.open("xb") as destination:
                while chunk := await upload.read(self.chunk_size):
                    total += len(chunk)
                    if total > self.max_upload_bytes:
                        raise payload_too_large(self.max_upload_bytes)
                    digest.update(chunk)
                    destination.write(chunk)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        finally:
            await upload.close()
        return total, digest.hexdigest()

    def write_json(self, key: str, payload: dict[str, Any]) -> None:
        content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        self.write_bytes(key, content)

    def write_bytes(self, key: str, content: bytes) -> None:
        target = self.resolve(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.write_bytes(content)
        os.replace(temporary, target)

    def move(self, source_key: str, destination_key: str) -> None:
        source = self.resolve(source_key)
        destination = self.resolve(destination_key)
        if not source.is_file():
            raise FileNotFoundError(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise FileExistsError(destination)
        shutil.move(str(source), str(destination))

    def delete_prefix(self, prefix: str) -> None:
        target = self.resolve(prefix)
        if target == self.root:
            raise bad_request("INVALID_STORAGE_PREFIX", "A raiz do storage não pode ser removida.")
        if target.exists():
            shutil.rmtree(target)

    def exists(self, key: str) -> bool:
        return self.resolve(key).exists()

    def ready(self) -> bool:
        return self.root.is_dir() and os.access(self.root, os.R_OK | os.W_OK)
