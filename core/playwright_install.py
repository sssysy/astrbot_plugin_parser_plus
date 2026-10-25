from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass

INSTALL_TIMEOUT_SECONDS = 15 * 60
MAX_OUTPUT_CHARS = 4000


@dataclass(frozen=True, slots=True)
class PlaywrightInstallResult:
    returncode: int | None
    output: str = ""
    timed_out: bool = False
    error_type: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


async def install_playwright_chromium(
    timeout: float = INSTALL_TIMEOUT_SECONDS,
) -> PlaywrightInstallResult:
    """使用当前 Python 环境安装 Playwright Chromium 及其系统依赖。"""
    command = [sys.executable, "-m", "playwright", "install"]
    if sys.platform.startswith("linux"):
        command.append("--with-deps")
    command.append("chromium")
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except (OSError, RuntimeError) as exc:
        return PlaywrightInstallResult(
            returncode=None,
            error_type=type(exc).__name__,
        )

    try:
        try:
            output_bytes, _ = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
            return PlaywrightInstallResult(returncode=None, timed_out=True)

        output = output_bytes.decode(errors="replace").strip()
        return PlaywrightInstallResult(
            returncode=process.returncode,
            output=output[-MAX_OUTPUT_CHARS:],
        )
    finally:
        # 命令处理被取消或异常退出时，避免留下子进程。
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()