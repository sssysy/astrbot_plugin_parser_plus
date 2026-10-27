from __future__ import annotations

import html
import re
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageFilter

from ...exception import ParseException


def clean_html(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def format_tags(tags_data: Any) -> str:
    if not isinstance(tags_data, dict):
        return ""
    tags = tags_data.get("tags", [])
    if not isinstance(tags, list):
        return ""
    parts: list[str] = []
    for tag_info in tags:
        if not isinstance(tag_info, dict):
            continue
        tag = str(tag_info.get("tag") or "").strip()
        if not tag:
            continue
        raw_translation = tag_info.get("translation")
        if isinstance(raw_translation, dict):
            translation = str(raw_translation.get("en") or "").strip()
        else:
            translation = str(raw_translation or "").strip()
        parts.append(f"#{tag}({translation})" if translation else f"#{tag}")
    return ", ".join(parts)


def build_text(body: dict[str, Any]) -> str:
    description = clean_html(str(body.get("description") or ""))
    tags_text = format_tags(body.get("tags"))
    parts: list[str] = []
    if description:
        parts.append(f"简介: {description}")
    if tags_text:
        parts.append(f"标签: {tags_text}")
    return "\n".join(parts)


def clean_novel_text(text: str) -> str:
    text = text.replace("[newpage]", "\n\n---\n\n")
    text = re.sub(r"\[\[jumpuri:\s*([^>]+?)\s*>[^\]]*\]\]", r"\1", text)
    text = re.sub(r"\[\[rb:\s*([^>]+?)\s*>[^\]]*\]\]", r"\1", text)
    return re.sub(r"\[\[jump:[^\]]*\]\]", "", text)


def parse_timestamp(date_str: str) -> int | None:
    if not date_str:
        return None
    try:
        value = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return int(value.timestamp())
    except (TypeError, ValueError):
        return None


def blur_image(image_path: Path, radius: int = 5) -> Path:
    output_path = image_path.parent / f"{image_path.stem}_blur{image_path.suffix}"
    with Image.open(image_path) as image:
        image.filter(ImageFilter.GaussianBlur(radius=radius)).save(output_path)
    return output_path


def images_to_pdf(image_paths: list[Path], output_path: Path) -> Path:
    if not image_paths:
        raise ParseException("图片列表为空")
    images: list[Image.Image] = []
    try:
        for image_path in image_paths:
            with Image.open(image_path) as image:
                images.append(image.convert("RGB"))
        images[0].save(
            output_path,
            "PDF",
            save_all=True,
            append_images=images[1:],
        )
    finally:
        for image in images:
            image.close()
    return output_path


def _zip_frames(
    zip_path: Path,
    frames: list[dict[str, Any]],
    *,
    mode: str,
) -> tuple[list[Image.Image], list[int]]:
    images: list[Image.Image] = []
    durations: list[int] = []
    try:
        with zipfile.ZipFile(zip_path) as archive:
            for frame in frames:
                file_name = str(frame.get("file") or "")
                if not file_name:
                    continue
                with archive.open(file_name) as source:
                    with Image.open(source) as image:
                        image.load()
                        images.append(image.convert(mode))
                try:
                    durations.append(max(int(frame.get("delay") or 100), 10))
                except (TypeError, ValueError):
                    durations.append(100)
    except (KeyError, OSError, zipfile.BadZipFile) as exc:
        for image in images:
            image.close()
        raise ParseException("Pixiv 动图帧提取失败") from exc
    if not images:
        raise ParseException("Pixiv 动图帧数据为空")
    return images, durations


def ugoira_to_gif(
    zip_path: Path,
    frames: list[dict[str, Any]],
    output_path: Path,
) -> Path:
    images, durations = _zip_frames(zip_path, frames, mode="RGBA")
    try:
        images[0].save(
            output_path,
            "GIF",
            save_all=True,
            append_images=images[1:],
            duration=durations,
            loop=0,
        )
    finally:
        for image in images:
            image.close()
    return output_path


def ugoira_to_pdf(
    zip_path: Path,
    frames: list[dict[str, Any]],
    output_path: Path,
) -> Path:
    images, _ = _zip_frames(zip_path, frames, mode="RGB")
    try:
        images[0].save(
            output_path,
            "PDF",
            save_all=True,
            append_images=images[1:],
        )
    finally:
        for image in images:
            image.close()
    return output_path
