"""公开帖子图片的本地缓存与 OCR。分析阶段只读本地文件，绝不补抓平台。"""

from datetime import datetime, timezone
import hashlib
from importlib.util import find_spec
from io import BytesIO
from pathlib import Path
import re
import threading
from urllib.parse import urlparse

from sqlalchemy import select

from app.config import settings
from app.models import SocialPostMediaModel, SocialSignalAnalysisModel

OCR_VERSION = "rapidocr-onnx-1.4.4-v1"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAX_IMAGE_BYTES = 8 * 1024 * 1024
_ocr_lock = threading.Lock()
_ocr_engine = None


def media_path(name: str) -> Path:
    """只接收本服务生成的哈希文件名，API 不能用路径参数读取任意文件。"""
    if not re.fullmatch(r"[a-f0-9]{64}\.png", name):
        raise ValueError("无效图片标识")
    root = Path(settings.social_media_dir).expanduser()
    if not root.is_absolute():
        root = PROJECT_ROOT / root
    return root.resolve() / name


def allowed_image_url(url: str) -> bool:
    """只下载公开页面给出的小红书 CDN 图片，不跟随重定向或访问任意内网地址。"""
    parsed = urlparse(url)
    try:
        return (parsed.scheme == "https" and not parsed.username and not parsed.password
                and parsed.port in {None, 443}
                and any(parsed.hostname == host or (parsed.hostname or "").endswith("." + host)
                        for host in ("xhscdn.com", "xhsimg.com")))
    except ValueError:
        return False


def cache_image(body: bytes, *, post_id: str, index: int, source_url: str) -> dict:
    """验证实际图片内容、限制像素，转成无 EXIF 的 PNG 后按内容寻址保存。"""
    from PIL import Image

    if len(body) > MAX_IMAGE_BYTES:
        raise ValueError("图片超过 8MB，未保存")
    with Image.open(BytesIO(body)) as image:
        if image.format not in {"JPEG", "PNG", "WEBP", "GIF"} or image.width * image.height > 25_000_000:
            raise ValueError("图片格式或尺寸超出限制")
        image.thumbnail((2400, 2400))
        output = BytesIO()
        image.convert("RGB").save(output, format="PNG")
    data = output.getvalue()
    digest = hashlib.sha256(data).hexdigest()
    path = media_path(digest + ".png")
    path.parent.mkdir(parents=True, exist_ok=True)
    # 同一内容的并发写入字节完全相同；文件名不含作者或平台令牌。
    if not path.exists():
        path.write_bytes(data)
    return {"media_id": hashlib.sha256(f"{post_id}:{index}:{digest}".encode()).hexdigest(),
            "post_id": post_id, "image_index": index, "source_url": source_url,
            "storage_name": path.name}


def recognize_image(path: Path) -> dict:
    """复用 CPU OCR 引擎；返回逐行置信度。OCR 是文字识别，不验证交易真实性。"""
    global _ocr_engine
    if find_spec("rapidocr_onnxruntime") is None:
        return {"status": "unavailable", "error": "未安装本地 OCR 依赖，请安装 requirements.txt"}
    if not path.is_file():
        return {"status": "error", "error": "本地图片文件缺失，需要恢复已缓存文件"}
    try:
        with _ocr_lock:
            if _ocr_engine is None:
                from rapidocr_onnxruntime import RapidOCR
                _ocr_engine = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=2)
            results, _ = _ocr_engine(str(path))
        lines = [{"text": str(item[1])[:500], "confidence": round(float(item[2]), 4)}
                 for item in (results or [])[:150]]
        return {"status": "completed" if lines else "empty", "lines": lines,
                "text": "\n".join(item["text"] for item in lines)[:8000],
                "version": OCR_VERSION, "error": ""}
    except Exception:
        # 不将可能含本机路径的底层异常暴露给网页。
        return {"status": "error", "error": "图片文字识别失败，可稍后重新分析", "version": OCR_VERSION}


class SocialMediaService:
    def __init__(self, database):
        self.database = database

    def save(self, media: list[dict]) -> None:
        """图片与帖子共用 ID，重复采集不覆盖已完成的 OCR。"""
        with self.database.session() as session, session.begin():
            for item in media:
                if session.get(SocialPostMediaModel, item["media_id"]) is None:
                    session.add(SocialPostMediaModel(**item, captured_at=datetime.now(timezone.utc),
                                                    recognition={"status": "pending"}))

    def for_posts(self, post_ids: list[str]) -> dict[str, list[dict]]:
        grouped = {post_id: [] for post_id in post_ids}
        if not post_ids:
            return grouped
        with self.database.session() as session:
            rows = session.scalars(select(SocialPostMediaModel).where(
                SocialPostMediaModel.post_id.in_(post_ids)).order_by(SocialPostMediaModel.image_index))
            for row in rows:
                grouped[row.post_id].append({"media_id": row.media_id, "image_index": row.image_index,
                    "image_url": f"/api/v1/market/social-signals/images/{row.media_id}",
                    "recognition": row.recognition or {"status": "pending"}})
        return grouped

    def path_for_id(self, media_id: str) -> Path | None:
        with self.database.session() as session:
            row = session.get(SocialPostMediaModel, media_id)
            return media_path(row.storage_name) if row else None

    def recognize_posts(self, post_ids: list[str]) -> dict:
        """只识别该窗口已有图片。失败允许下次手动重试，成功结果可直接复用。"""
        with self.database.session() as session:
            rows = [(row.media_id, row.storage_name) for row in session.scalars(
                select(SocialPostMediaModel).where(SocialPostMediaModel.post_id.in_(post_ids)))
                if (row.recognition or {}).get("status") not in {"completed", "empty"}]
        for media_id, name in rows:
            result = recognize_image(media_path(name))
            with self.database.session() as session, session.begin():
                media = session.get(SocialPostMediaModel, media_id)
                changed = media.recognition != result
                media.recognition = result
                analysis = session.get(SocialSignalAnalysisModel, media.post_id)
                if changed and analysis:
                    # 旧判断基于无 OCR 的输入，不删记录，但标记为需要重新分析。
                    analysis.input_hash = ""
        return {"image_count": len(rows)}
