import io
import logging
import os
import shutil

from PIL import Image

import config

logger = logging.getLogger(__name__)


def is_valid(data: bytes) -> bool:
    """Полная проверка картинки: Pillow декодирует её целиком, битые и обрезанные файлы отсекаются."""
    if not data or len(data) < 64:
        return False
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
        return True
    except Exception:
        return False


def _dir(account_id: int) -> str:
    return os.path.join(config.IMAGES_DIR, str(account_id))


def _ext(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return "jpg"


def save(account_id: int, images: list[bytes]):
    """Заменяет сохранённые картинки лота аккаунта."""
    if not images or not all(is_valid(data) for data in images):
        raise ValueError("Картинка повреждена или не является изображением")
    remove(account_id)
    os.makedirs(_dir(account_id), exist_ok=True)
    for i, data in enumerate(images, start=1):
        with open(os.path.join(_dir(account_id), f"{i}.{_ext(data)}"), "wb") as f:
            f.write(data)


def load(account_id: int) -> list[bytes]:
    folder = _dir(account_id)
    if not os.path.isdir(folder):
        return []
    names = sorted(os.listdir(folder), key=lambda n: int(n.split(".")[0]) if n.split(".")[0].isdigit() else 0)
    result = []
    for name in names:
        with open(os.path.join(folder, name), "rb") as f:
            data = f.read()
        if is_valid(data):
            result.append(data)
        else:
            logger.warning(f"[images] пропущена битая картинка {folder}/{name}")
    return result


def count(account_id: int) -> int:
    return len(load(account_id))


def remove(account_id: int):
    shutil.rmtree(_dir(account_id), ignore_errors=True)
