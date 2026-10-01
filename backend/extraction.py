from __future__ import annotations

import io
import os
import threading
from pathlib import Path
from typing import Any

import fitz
from PIL import Image

MODELS_DIR = Path(__file__).resolve().parent / "ocr_models"


class TextExtractor:
    """Extracts native PDF text first and uses local OCR only when required."""

    def __init__(self) -> None:
        self._engine: Any | None = None
        self._engine_lock = threading.Lock()
        MODELS_DIR.mkdir(exist_ok=True)
        os.environ.setdefault("PADDLE_OCR_BASE_DIR", str(MODELS_DIR))
        os.environ.setdefault("PADDLE_PDX_CACHE_HOME", str(MODELS_DIR / "paddlex"))
        # Paddle's current Windows CPU wheel can select an unsupported oneDNN
        # execution path for OCR models; use the portable CPU inference path.
        os.environ.setdefault("FLAGS_use_mkldnn", "0")
        # Paddle itself resolves its cache from the Windows profile directory.
        # Redirect that process-local lookup before Paddle is imported so all OCR
        # runtime data remains below the backend directory.
        paddle_home = MODELS_DIR / "paddle_home"
        paddle_home.mkdir(exist_ok=True)
        os.environ["USERPROFILE"] = str(paddle_home)
        os.environ["HOMEDRIVE"] = paddle_home.drive
        os.environ["HOMEPATH"] = paddle_home.root

    def extract_pdf(self, path: Path) -> tuple[str, int, int]:
        document = fitz.open(path)
        try:
            page_count = document.page_count
            sections: list[str] = []
            ocr_page_count = 0
            for page_number, page in enumerate(document, start=1):
                native_text = page.get_text("text").strip()
                if len(native_text) >= 20:
                    page_text = native_text
                else:
                    pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                    page_text = self._ocr_bytes(pixmap.tobytes("png"))
                    if page_text:
                        ocr_page_count += 1
                    elif native_text:
                        page_text = native_text
                sections.append(f"--- Page {page_number} ---\n{page_text.strip()}")
            return "\n\n".join(sections).strip(), page_count, ocr_page_count
        finally:
            document.close()

    def extract_image(self, path: Path) -> tuple[str, int, int]:
        return self._ocr_bytes(path.read_bytes()), 1, 1

    def pdf_page_count(self, path: Path) -> int:
        document = fitz.open(path)
        try:
            return document.page_count
        finally:
            document.close()

    def _ocr_bytes(self, image_bytes: bytes) -> str:
        with Image.open(io.BytesIO(image_bytes)) as image:
            image.load()
            import numpy as np

            image_array = np.array(image.convert("RGB"))

        engine = self._get_engine()
        if hasattr(engine, "ocr"):
            try:
                return self._legacy_text(engine.ocr(image_array, cls=True))
            except TypeError:
                # PaddleOCR 3 keeps an `ocr` alias but removed the legacy `cls` option.
                return self._v3_text(engine.predict(image_array))
        return self._v3_text(engine.predict(image_array))

    def _get_engine(self) -> Any:
        with self._engine_lock:
            if self._engine is not None:
                return self._engine
            try:
                from paddleocr import PaddleOCR
            except ModuleNotFoundError as error:
                raise RuntimeError(
                    "PaddleOCR is not installed in the active backend environment. "
                    "Start the API with backend\\.venv\\Scripts\\python.exe."
                ) from error

            try:
                self._engine = PaddleOCR(
                    lang="en",
                    use_doc_orientation_classify=False,
                    use_doc_unwarping=False,
                    use_textline_orientation=False,
                    device="cpu",
                    enable_mkldnn=False,
                )
            except TypeError:
                self._engine = PaddleOCR(use_angle_cls=True, lang="en", use_gpu=False)
            return self._engine

    @staticmethod
    def _legacy_text(result: Any) -> str:
        lines: list[str] = []
        for page in result or []:
            for line in page or []:
                try:
                    lines.append(str(line[1][0]))
                except (IndexError, KeyError, TypeError):
                    continue
        return "\n".join(lines).strip()

    @staticmethod
    def _v3_text(result: Any) -> str:
        lines: list[str] = []
        for item in result or []:
            data: Any = item
            if hasattr(item, "json"):
                data = item.json
                if callable(data):
                    data = data()
            if isinstance(data, str):
                import json
                data = json.loads(data)
            if isinstance(data, dict):
                payload = data.get("res", data)
                values = payload.get("rec_texts") or payload.get("text") or []
                if isinstance(values, str):
                    lines.append(values)
                else:
                    lines.extend(str(value) for value in values)
        return "\n".join(lines).strip()
