from __future__ import annotations

import io
import shutil
import tempfile
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, Iterator

from fastapi import HTTPException
from fastapi.responses import FileResponse, Response
from PIL import Image, ImageOps

from services.config import config
from services.image_storage_service import IMAGE_EXTENSIONS, image_storage_service
from services.image_tags_service import load_tags, remove_tags_many
from utils.log import logger
from utils.image_formats import IMAGE_MIME_TYPES, SVG_HEADERS, image_format, validate_svg

THUMBNAIL_SIZE = (320, 320)
IMAGE_CLEANUP_LOCK = threading.Lock()
THUMBNAIL_LOCKS = [threading.Lock() for _ in range(32)]


def _cleanup_empty_dirs(root: Path) -> None:
    for path in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        try:
            path.rmdir()
        except OSError:
            pass


def _safe_relative_path(path: str) -> str:
    value = str(path or "").strip().replace("\\", "/").lstrip("/")
    if not value:
        raise HTTPException(status_code=404, detail="image not found")
    parts = Path(value).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise HTTPException(status_code=404, detail="image not found")
    return Path(*parts).as_posix()


def _safe_image_path(relative_path: str) -> Path:
    rel = _safe_relative_path(relative_path)
    root = config.images_dir.resolve()
    path = (root / rel).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="image not found") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="image not found")
    return path


def get_image_response(relative_path: str) -> FileResponse | Response:
    headers = {
        **(SVG_HEADERS if Path(relative_path).suffix.lower() == ".svg" else {}),
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "*",
    }
    if image_storage_service.has_local(relative_path):
        path = _safe_image_path(relative_path)
        if path.suffix.lower() != ".svg":
            return FileResponse(path, headers=headers)
        if path.stat().st_size > 10 * 1024 * 1024:
            raise HTTPException(status_code=422, detail="oversized SVG image")
        payload = path.read_bytes()
        try:
            validate_svg(payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="unsafe or invalid SVG image") from exc
        return Response(content=payload, media_type="image/svg+xml", headers=headers)
    payload = image_storage_service.get_bytes(relative_path)
    actual_format = image_format(payload)
    if actual_format == "svg":
        headers.update(SVG_HEADERS)
    return Response(content=payload, media_type=IMAGE_MIME_TYPES[actual_format], headers=headers)


def _thumbnail_path(relative_path: str) -> Path:
    rel = _safe_relative_path(relative_path)
    root = config.image_thumbnails_dir.resolve()
    path = (root / f"{rel}.png").resolve()
    if not path.is_relative_to(root):
        raise HTTPException(status_code=404, detail="image not found")
    return path


def thumbnail_url(base_url: str, relative_path: str) -> str:
    return f"{base_url.rstrip('/')}/image-thumbnails/{_safe_relative_path(relative_path)}"


def _image_dimensions(path: Path) -> tuple[int, int] | None:
    try:
        with Image.open(path) as image:
            return image.size
    except Exception:
        return None


def ensure_thumbnail(relative_path: str) -> Path:
    rel = _safe_relative_path(relative_path)
    with THUMBNAIL_LOCKS[hash(rel) % len(THUMBNAIL_LOCKS)]:
        return _ensure_thumbnail(rel)


def _ensure_thumbnail(relative_path: str) -> Path:
    target = _thumbnail_path(relative_path)
    source_mtime = 0.0
    source: Path | None = None
    if image_storage_service.has_local(relative_path):
        source = _safe_image_path(relative_path)
        source_mtime = source.stat().st_mtime
    if target.exists() and (not source_mtime or target.stat().st_mtime >= source_mtime):
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        image_source = source if source is not None else io.BytesIO(image_storage_service.get_bytes(relative_path))
        with Image.open(image_source) as image:
            image = ImageOps.exif_transpose(image)
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            image.thumbnail(THUMBNAIL_SIZE, Image.Resampling.LANCZOS)
            with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
            image.save(temporary, format="PNG", optimize=True)
            temporary.replace(target)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=422, detail="failed to create thumbnail") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target


def get_thumbnail_response(relative_path: str) -> FileResponse | Response:
    if Path(relative_path).suffix.lower() == ".svg":
        return get_image_response(relative_path)
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "*",
    }
    return FileResponse(ensure_thumbnail(relative_path), headers=headers)


def get_image_download_response(relative_path: str) -> FileResponse | Response:
    cors_headers = {
        **(SVG_HEADERS if Path(relative_path).suffix.lower() == ".svg" else {}),
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "*",
    }
    if image_storage_service.has_local(relative_path):
        path = _safe_image_path(relative_path)
        headers = {**cors_headers, "Content-Disposition": f'attachment; filename="{path.name}"'}
        return FileResponse(path, filename=path.name, headers=headers)
    rel = _safe_relative_path(relative_path)
    headers = {
        **cors_headers,
        "Content-Disposition": f'attachment; filename="{Path(rel).name}"',
    }
    payload = image_storage_service.get_bytes(rel)
    actual_format = image_format(payload)
    if actual_format == "svg":
        headers.update(SVG_HEADERS)
    return Response(
        content=payload,
        media_type=IMAGE_MIME_TYPES[actual_format],
        headers=headers,
    )


def cleanup_image_thumbnails() -> int:
    thumbnails_root = config.image_thumbnails_dir
    indexed = image_storage_service.index_snapshot()
    removed = 0
    for path in thumbnails_root.rglob("*"):
        if not path.is_file() or path.suffix == ".tmp":
            continue
        rel = path.relative_to(thumbnails_root).as_posix()
        source_rel = rel[:-4] if rel.endswith(".png") else rel
        if not _image_exists(source_rel, indexed):
            path.unlink(missing_ok=True)
            removed += 1
    _cleanup_empty_dirs(thumbnails_root)
    stale_tags = [
        rel for rel in load_tags()
        if not _image_exists(rel, indexed)
    ]
    remove_tags_many(stale_tags)
    return removed


def _image_exists(rel: str, indexed: dict[str, dict[str, object]]) -> bool:
    try:
        return image_storage_service.has_local(rel) or bool(indexed.get(rel, {}).get("webdav"))
    except HTTPException:
        return False


def list_images(base_url: str, start_date: str = "", end_date: str = "") -> dict[str, object]:
    all_tags = load_tags()
    items = [
        {
            **item,
            "url": str(item.get("url") or f"{base_url.rstrip('/')}/images/{item['path']}"),
            "thumbnail_url": thumbnail_url(base_url, str(item["path"])),
            "tags": all_tags.get(str(item["path"]), []),
        }
        for item in image_storage_service.list_items(base_url, start_date, end_date)
    ]
    groups: dict[str, list[dict[str, object]]] = {}
    for item in items:
        groups.setdefault(str(item["date"]), []).append(item)
    return {"items": items, "groups": [{"date": key, "items": value} for key, value in groups.items()]}


def delete_images(paths: list[str] | None = None, start_date: str = "", end_date: str = "", all_matching: bool = False) -> dict[str, int]:
    root = config.images_dir.resolve()
    targets = [
        str(item["path"])
        for item in image_storage_service.list_items("", start_date=start_date, end_date=end_date, refresh=True)
    ] if all_matching else (paths or [])
    targets = list(dict.fromkeys(_safe_relative_path(rel) for rel in targets))
    for rel in targets:
        if Path(rel).suffix.lower() not in IMAGE_EXTENSIONS:
            raise HTTPException(status_code=404, detail="image not found")
        if not (root / rel).resolve().is_relative_to(root):
            raise HTTPException(status_code=404, detail="image not found")
        _thumbnail_path(rel)
    with IMAGE_CLEANUP_LOCK:
        try:
            removed = image_storage_service.delete_many(targets)
        finally:
            indexed = image_storage_service.index_snapshot()
            _remove_image_metadata([rel for rel in targets if not _image_exists(rel, indexed)])
    _cleanup_empty_dirs(root)
    _cleanup_empty_dirs(config.image_thumbnails_dir)
    return {"removed": len(removed)}


def _remove_image_metadata(paths: list[str]) -> None:
    root = config.image_thumbnails_dir.resolve()
    for item in paths:
        for thumbnail in (_thumbnail_path(item), config.image_thumbnails_dir / _safe_relative_path(item)):
            if thumbnail.resolve().is_relative_to(root) and thumbnail.is_file():
                thumbnail.unlink(missing_ok=True)
    remove_tags_many(paths)


def download_images_zip(paths: list[str]) -> BinaryIO:
    root = config.images_dir.resolve()
    buf = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
    added = 0
    used_names: set[str] = set()
    try:
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for item in paths:
                rel = _safe_relative_path(item)
                path = (root / rel).resolve()
                if not path.is_relative_to(root):
                    continue
                name = path.name
                if name in used_names:
                    counter = 2
                    while f"{path.stem}_{counter}{path.suffix}" in used_names:
                        counter += 1
                    name = f"{path.stem}_{counter}{path.suffix}"
                if path.is_file():
                    zf.write(path, name)
                else:
                    try:
                        payload = image_storage_service.get_bytes(rel)
                    except Exception:
                        continue
                    zf.writestr(name, payload)
                used_names.add(name)
                added += 1
        if added == 0:
            raise HTTPException(status_code=404, detail="no images found")
        buf.seek(0)
        return buf
    except BaseException:
        buf.close()
        raise


def iter_zip_chunks(buffer: BinaryIO) -> Iterator[bytes]:
    try:
        while chunk := buffer.read(64 * 1024):
            yield chunk
    finally:
        buffer.close()


def storage_stats() -> dict:
    usage = shutil.disk_usage(config.images_dir)
    total_mb = usage.total // (1024 * 1024)
    used_mb = usage.used // (1024 * 1024)
    free_mb = usage.free // (1024 * 1024)

    image_count = 0
    image_size = 0
    for p in config.images_dir.rglob("*"):
        if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS:
            try:
                image_size += p.stat().st_size
                image_count += 1
            except FileNotFoundError:
                continue

    return {
        "disk_total_mb": total_mb,
        "disk_used_mb": used_mb,
        "disk_free_mb": free_mb,
        "image_count": image_count,
        "image_size_mb": image_size // (1024 * 1024),
        "image_size_bytes": image_size,
    }


def compress_images(quality: int = 60) -> dict:
    """重新压缩所有图片，返回节省的空间"""
    with IMAGE_CLEANUP_LOCK:
        return _compress_images()


def _compress_images() -> dict:
    saved = 0
    count = 0
    for p in sorted(config.images_dir.rglob("*.png")):
        if not p.is_file():
            continue
        try:
            orig = p.stat().st_size
            with Image.open(p) as img:
                img = ImageOps.exif_transpose(img)
                img.save(str(p) + ".tmp", format="PNG", optimize=True)
            new_size = Path(str(p) + ".tmp").stat().st_size
            if new_size < orig:
                Path(str(p) + ".tmp").replace(p)
                saved += orig - new_size
                count += 1
            else:
                Path(str(p) + ".tmp").unlink()
        except Exception:
            pass
        finally:
            Path(str(p) + ".tmp").unlink(missing_ok=True)
    return {"compressed": count, "saved_bytes": saved, "saved_mb": saved // (1024 * 1024)}


def delete_to_target(target_free_mb: int, dry_run: bool = False) -> dict:
    """删除最旧的图片直到剩余空间达到 target_free_mb"""
    with IMAGE_CLEANUP_LOCK:
        return _delete_to_target(target_free_mb, dry_run)


def _delete_to_target(target_free_mb: int, dry_run: bool) -> dict:
    usage = shutil.disk_usage(config.images_dir)
    current_free = usage.free // (1024 * 1024)
    if current_free >= target_free_mb and not dry_run:
        return {"removed": 0, "current_free_mb": current_free, "target_free_mb": target_free_mb, "done": True}

    files = sorted(
        (p for p in config.images_dir.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS),
        key=lambda p: p.stat().st_mtime,
    )
    removed = 0
    freed = 0
    removed_paths: list[str] = []
    for p in files:
        if current_free + freed // (1024 * 1024) >= target_free_mb:
            break
        try:
            size = p.stat().st_size
        except FileNotFoundError:
            continue
        if not dry_run:
            rel = p.relative_to(config.images_dir).as_posix()
            removed_paths.append(rel)
        freed += size
        removed += 1

    if not dry_run:
        # Free local disk only; keep an indexed WebDAV copy accessible.
        try:
            image_storage_service.delete_many(removed_paths, local_only=True)
        finally:
            indexed = image_storage_service.index_snapshot()
            _remove_image_metadata([rel for rel in removed_paths if not _image_exists(rel, indexed)])
        _cleanup_empty_dirs(config.images_dir)
        _cleanup_empty_dirs(config.image_thumbnails_dir)
        current_free = shutil.disk_usage(config.images_dir).free // (1024 * 1024)

    resulting_free = current_free + (freed // (1024 * 1024)) if dry_run else current_free

    return {
        "removed": removed,
        "freed_mb": freed // (1024 * 1024),
        "target_free_mb": target_free_mb,
        "current_free_mb": resulting_free,
        "done": resulting_free >= target_free_mb,
        "dry_run": dry_run,
    }


def cleanup_expired_images(retention_days: int | None = None) -> int:
    days = config.image_retention_days if retention_days is None else retention_days
    cutoff = time.time() - days * 86400
    with IMAGE_CLEANUP_LOCK:
        targets: list[str] = []
        for item in image_storage_service.list_items("", refresh=True):
            rel = str(item["path"])
            local = config.images_dir / rel
            try:
                modified = local.stat().st_mtime
            except FileNotFoundError:
                try:
                    modified = datetime.strptime(str(item.get("created_at") or ""), "%Y-%m-%d %H:%M:%S").timestamp()
                except ValueError:
                    continue
            if modified < cutoff:
                targets.append(rel)
        try:
            removed = image_storage_service.delete_many(targets)
        finally:
            indexed = image_storage_service.index_snapshot()
            _remove_image_metadata([rel for rel in targets if not _image_exists(rel, indexed)])
        _cleanup_empty_dirs(config.images_dir)
        cleanup_image_thumbnails()
        return len(removed)


def _auto_cleanup_worker(stop_event: threading.Event) -> None:
    """后台线程：每30分钟检查存储，空间低于阈值自动清理最旧图片"""
    while not stop_event.is_set():
        try:
            cleanup_expired_images()
            min_free_mb = config.image_min_free_mb
            usage = shutil.disk_usage(config.images_dir)
            free_mb = usage.free // (1024 * 1024)
            if free_mb < min_free_mb:
                logger.info({"event": "image_auto_cleanup", "free_mb": free_mb, "min_free_mb": min_free_mb})
                result = delete_to_target(min_free_mb)
                logger.info({"event": "image_auto_cleanup_done", **result})
        except Exception as exc:
            logger.warning({"event": "image_auto_cleanup_failed", "error": str(exc)})
        if stop_event.wait(1800):
            break


def start_image_cleanup_scheduler(stop_event: threading.Event) -> threading.Thread:
    t = threading.Thread(target=_auto_cleanup_worker, args=(stop_event,), daemon=True, name="image-cleanup")
    t.start()
    return t
