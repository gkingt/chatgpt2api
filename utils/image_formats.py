from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET

from fastapi import HTTPException
from PIL import Image

IMAGE_MIME_TYPES = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp", "gif": "image/gif", "svg": "image/svg+xml"}
SVG_HEADERS = {"Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'", "X-Content-Type-Options": "nosniff"}


def image_format(payload: bytes) -> str:
    if payload.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<"):
        validate_svg(payload)
        return "svg"
    try:
        with Image.open(io.BytesIO(payload)) as image:
            return {"PNG": "png", "JPEG": "jpeg", "WEBP": "webp", "GIF": "gif"}.get(image.format, "png")
    except Exception:
        return "png"


def validate_svg(payload: bytes) -> None:
    # SVGs are served on the application's origin: never allow active or remote content.
    if len(payload) > 10 * 1024 * 1024:
        raise ValueError("Unsafe or oversized SVG output")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("SVG output must be UTF-8") from exc
    if re.search(r"<!\s*(DOCTYPE|ENTITY)|<\?(?!xml\s)", text, re.I):
        raise ValueError("Unsafe SVG declaration")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError("Invalid SVG XML") from exc
    allowed = {"svg", "g", "defs", "title", "desc", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "text", "tspan", "textPath", "linearGradient", "radialGradient", "stop", "clipPath", "mask", "pattern", "use", "symbol", "marker", "style"}
    if root.tag.split("}")[-1] != "svg":
        raise ValueError("Invalid SVG root")
    for node in root.iter():
        namespace, _, name = node.tag.rpartition("}")
        if namespace not in {"", "{http://www.w3.org/2000/svg"} or name not in allowed:
            raise ValueError("SVG output must contain static vector content only")
        if name == "style" and len(node):
            raise ValueError("SVG styles must be plain static CSS")
        for key, value in node.attrib.items():
            name = key.split("}")[-1].lower()
            if name.startswith("on") or name == "base" or (name == "href" and not value.strip().startswith("#")):
                raise ValueError("Unsafe SVG attribute")
        values = [*node.attrib.values(), node.text or ""]
        for value in values:
            if re.search(r"@|\\|/\*|javascript\s*:|expression\s*\(|(?:https?|data|file)\s*:", value, re.I):
                raise ValueError("External or active SVG resources are not supported")
            for reference in re.findall(r"url\s*\((.*?)\)", value, re.I):
                if not reference.strip(" \t\r\n\"'").startswith("#"):
                    raise ValueError("External SVG resources are not supported")


def normalize_image_options(fields: dict, *, edit: bool = False) -> dict:
    def invalid(message: str):
        raise HTTPException(status_code=400, detail={"error": message})

    output_format = str(fields.get("output_format") or "png").lower().strip()
    if output_format not in IMAGE_MIME_TYPES:
        invalid("output_format must be png, jpeg, webp, gif or svg")
    options = {"output_format": output_format}
    complexity = fields.get("complexity")
    if complexity is not None:
        if not isinstance(complexity, str) or complexity not in {"low", "medium", "high"}:
            invalid("complexity must be low, medium or high")
        options["complexity"] = complexity
    if output_format == "svg" and (edit or fields.get("n", 1) not in (None, 1, "1")):
        invalid("SVG output supports generation only, with n=1")
    if output_format in {"gif", "svg"} and (fields.get("stream") or fields.get("partial_images") is not None or fields.get("output_compression") is not None):
        invalid("GIF/SVG output does not support stream, partial_images or output_compression")
    if output_format not in {"gif", "svg"} and fields.get("output_compression") is not None:
        invalid("output_compression is not supported by the account-pool image protocol")
    for key, default, minimum, maximum in (("frame_count", 9, 2, 16), ("frame_duration_ms", 200, 20, 2000)):
        raw = fields.get(key)
        if raw is not None and output_format != "gif":
            invalid(f"{key} is only supported for GIF output")
        if output_format == "gif":
            try:
                value = int(str(default if raw is None else raw))
            except (ValueError, TypeError):
                invalid(f"{key} must be an integer")
            if not minimum <= value <= maximum:
                invalid(f"{key} must be between {minimum} and {maximum}")
            options[key] = ((value + 5) // 10) * 10 if key == "frame_duration_ms" else value
    size = str(fields.get("size") or "auto")
    if output_format == "gif" and size != "auto":
        match = re.fullmatch(r"([1-9]\d*)x([1-9]\d*)", size)
        if not match or any(int(part) % 16 for part in match.groups()):
            invalid("GIF size dimensions must be positive multiples of 16; use auto otherwise")
    return options


def prepare_image_output(payload: bytes, requested: str | None) -> tuple[bytes, str]:
    actual = image_format(payload)
    if requested in {"gif", "svg"}:
        if actual != requested:
            raise ValueError(f"Upstream returned {actual}, not requested {requested}; no format fallback was applied")
        if requested == "gif":
            with Image.open(io.BytesIO(payload)) as image:
                if getattr(image, "n_frames", 1) < 2:
                    raise ValueError("Upstream returned a static GIF instead of an animation")
    elif requested and actual != requested:
        if actual in {"gif", "svg"}:
            raise ValueError("Refusing to flatten animated or vector output into a raster image")
        with Image.open(io.BytesIO(payload)) as image:
            if getattr(image, "n_frames", 1) > 1:
                raise ValueError("Refusing to flatten animated output")
            output = io.BytesIO()
            image.convert("RGB" if requested == "jpeg" else "RGBA").save(output, format=requested.upper())
            payload, actual = output.getvalue(), requested
    return payload, actual
