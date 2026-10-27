"""FFmpeg/FFprobe 可执行文件发现。"""

from __future__ import annotations

import shutil
from pathlib import Path

from astrbot.api import logger


class FFmpegUnavailable(RuntimeError):
    """本机没有可用的 ffmpeg。"""


_FFMPEG_PATH: str | None = None


def _imageio_ffmpeg_path() -> str | None:
    """返回 imageio-ffmpeg 自带的 ffmpeg，未安装时返回 None。"""
    try:
        import imageio_ffmpeg
    except ImportError:
        return None
    try:
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (OSError, RuntimeError, ValueError):
        return None


def get_ffmpeg_path() -> str:
    """优先系统 ffmpeg，其次 imageio-ffmpeg 内置版本。"""
    global _FFMPEG_PATH
    if _FFMPEG_PATH:
        return _FFMPEG_PATH

    system = shutil.which("ffmpeg")
    if system:
        _FFMPEG_PATH = system
        return system

    bundled = _imageio_ffmpeg_path()
    if bundled and Path(bundled).is_file():
        logger.info(f"[媒体] 使用 imageio-ffmpeg 内置 ffmpeg: {Path(bundled).name}")
        _FFMPEG_PATH = bundled
        return bundled

    raise FFmpegUnavailable(
        "未找到 ffmpeg。请安装系统 FFmpeg，或运行 "
        "`pip install imageio-ffmpeg` 后重启插件。"
    )


def get_ffprobe_path() -> str | None:
    """返回系统 ffprobe；imageio-ffmpeg 通常不附带 ffprobe。"""
    return shutil.which("ffprobe")


async def run_ffprobe(*args: str) -> tuple[int, bytes, bytes]:
    """运行 ffprobe；没有 ffprobe 时返回可降级的结果。"""
    executable = get_ffprobe_path()
    if executable is None:
        return 127, b"", b"ffprobe not found"
    import asyncio

    process = await asyncio.create_subprocess_exec(
        executable,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    return process.returncode or 0, stdout, stderr


def reset_ffmpeg_cache() -> None:
    """清理进程级缓存，便于配置或运行环境变化后重新探测。"""
    global _FFMPEG_PATH
    _FFMPEG_PATH = None
