#!/usr/bin/env python3
"""
独立「维修」Telegram 小机器人：/edit、/ask、/log。

- /edit：aider 改指定文件，成功后 supervisorctl restart。
- /ask：aider --message「问题」、无文件参数，全局排查问答，不重启。
- /log：tail -n 50 服务 stderr 日志，回传（过长则仅保留末尾 4000 字符）。

与主程序 cedarstar（webhook 大机器人）分离部署：
- 推荐使用独立 Bot：在 .env 设置 REPAIR_TELEGRAM_BOT_TOKEN；
- 若与主机器人共用 TELEGRAM_BOT_TOKEN，须停用主进程的 Telegram webhook，否则 getUpdates 会冲突。

环境变量（均在 .env 或环境中配置即可，不修改 config.py）：
  REPAIR_TELEGRAM_BOT_TOKEN   优先；否则读 TELEGRAM_BOT_TOKEN
  REPAIR_TELEGRAM_PROXY       优先；否则读 TELEGRAM_PROXY（访问 api.telegram.org）
  REPAIR_ALLOWED_CHAT_IDS     可选，逗号分隔的数字 chat_id；未设置则不限制
  REPAIR_AIDER_BIN            默认 aider
  REPAIR_AIDER_TIMEOUT_SEC    aider subprocess.run 超时（秒），默认 3600（/edit 与 /ask 共用）
  REPAIR_SUPERVISOR_PROGRAM   默认 cedarstar
  REPAIR_SUPERVISORCTL_TIMEOUT_SEC  默认 120
  REPAIR_CEDARSTAR_ERR_LOG    cedarstar stderr 日志绝对路径；默认与仓库 supervisord.conf 一致：
                                /var/log/supervisor/cedarstar.err.log
  REPAIR_TAIL_TIMEOUT_SEC     tail 子进程超时（秒），默认 30

# repair smoke test
用法：在项目根执行  python3 repair_bot.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Optional, Sequence, Tuple

from dotenv import load_dotenv
from telegram import Message, Update
from telegram.constants import ChatAction
from telegram.error import NetworkError as TelegramNetworkError
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest

_PROJECT_ROOT = Path(__file__).resolve().parent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("repair_bot")


def _exc_detail(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


def _repair_token() -> str | None:
    return (os.getenv("REPAIR_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip() or None


def _repair_proxy() -> Optional[str]:
    v = (os.getenv("REPAIR_TELEGRAM_PROXY") or os.getenv("TELEGRAM_PROXY") or "").strip()
    return v or None


def _allowed_chat_ids() -> Optional[set[int]]:
    raw = (os.getenv("REPAIR_ALLOWED_CHAT_IDS") or "").strip()
    if not raw:
        return None
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            logger.warning("忽略非法 REPAIR_ALLOWED_CHAT_IDS 片段: %r", part)
    return out or None


def _is_chat_allowed(chat_id: int | None) -> bool:
    allowed = _allowed_chat_ids()
    if allowed is None:
        return True
    if chat_id is None:
        return False
    return chat_id in allowed


async def _reply_unauthorized(message: Message) -> None:
    try:
        await message.reply_text("⛔ 当前 chat 未授权使用此机器人。")
    except Exception:
        pass


_MSG_AIDER_BUSY_ASK = "🔍 宝宝稍等，小狗的赛博手术刀正在扫描代码…"
_MSG_AIDER_BUSY_EDIT = "🔍 宝宝稍等，小狗的赛博手术刀正在改写文件…"
_MSG_AIDER_TIMEOUT = "手术超时，请稍后重试。"


async def _before_aider_subprocess(
    context: ContextTypes.DEFAULT_TYPE,
    message: Message,
    *,
    edit_mode: bool,
) -> None:
    """在阻塞的 aider subprocess.run 之前：正在输入 + 提示文案。"""
    cid = message.chat_id
    try:
        await context.bot.send_chat_action(chat_id=cid, action=ChatAction.TYPING)
    except Exception as e:
        logger.warning("send_chat_action(TYPING) 失败: %s", _exc_detail(e))
    hint = _MSG_AIDER_BUSY_EDIT if edit_mode else _MSG_AIDER_BUSY_ASK
    try:
        await message.reply_text(hint)
    except Exception as e:
        logger.warning("发送 Aider 前置提示失败: %s", _exc_detail(e))


async def _before_supervisor_restart(
    context: ContextTypes.DEFAULT_TYPE, message: Message
) -> None:
    try:
        await context.bot.send_chat_action(
            chat_id=message.chat_id, action=ChatAction.TYPING
        )
    except Exception as e:
        logger.warning("send_chat_action(TYPING) supervisor 前失败: %s", _exc_detail(e))
    try:
        await message.reply_text("⚙️ 正在重启 supervisor 中的主服务…")
    except Exception as e:
        logger.warning("发送 supervisor 前置提示失败: %s", _exc_detail(e))


async def _before_tail_log(
    context: ContextTypes.DEFAULT_TYPE, message: Message
) -> None:
    try:
        await context.bot.send_chat_action(
            chat_id=message.chat_id, action=ChatAction.TYPING
        )
    except Exception as e:
        logger.warning("send_chat_action(TYPING) tail 前失败: %s", _exc_detail(e))


def parse_edit_command_from_args(args: Sequence[str]) -> Tuple[str, str]:
    args_list = list(args or [])
    if len(args_list) < 2:
        raise ValueError(
            "用法：/edit <相对项目根的文件路径> <修改说明>\n"
            "示例：/edit main.py 帮我把背景改成粉色"
        )
    target = (args_list[0] or "").strip()
    instruction = " ".join(args_list[1:]).strip()
    if not target:
        raise ValueError("目标文件名为空。")
    if not instruction:
        raise ValueError("修改要求不能为空。")
    return target, instruction


def parse_ask_from_args(args: Sequence[str]) -> str:
    question = " ".join((a or "").strip() for a in args).strip()
    if not question:
        raise ValueError(
            "用法：/ask <问题>\n"
            "示例：/ask 为什么健康检查接口一直超时"
        )
    return question


def resolve_safe_relative_path(filename: str) -> str:
    raw = (filename or "").strip().replace("\\", "/")
    if not raw or raw.startswith("/"):
        raise ValueError("请使用相对项目根目录的路径，例如 bot/foo.py")
    parts = Path(raw).parts
    if ".." in parts:
        raise ValueError("不允许在路径中使用 ..")
    candidate = (_PROJECT_ROOT / raw).resolve()
    try:
        candidate.relative_to(_PROJECT_ROOT)
    except ValueError:
        raise ValueError("目标路径必须位于项目目录内。") from None
    if candidate.exists():
        if not candidate.is_file():
            raise ValueError("目标路径存在但不是普通文件。")
    else:
        parent = candidate.parent
        if not parent.exists() or not parent.is_dir():
            raise ValueError("文件尚不存在时，其父目录必须已存在。")
        try:
            parent.resolve().relative_to(_PROJECT_ROOT)
        except ValueError:
            raise ValueError("父目录必须位于项目目录内。") from None
    return raw


def _truncate_for_telegram(s: str, max_len: int = 3800) -> str:
    s = s or ""
    if len(s) <= max_len:
        return s
    return s[: max_len - 20] + "\n\n…（输出过长已截断）"


def _aider_bin_and_timeout() -> Tuple[str, float]:
    aider_bin = (os.getenv("REPAIR_AIDER_BIN") or "aider").strip() or "aider"
    # 默认 3600s；可通过 REPAIR_AIDER_TIMEOUT_SEC 覆盖；下限 30s 避免误配成 0
    timeout = max(30.0, _env_float("REPAIR_AIDER_TIMEOUT_SEC", 3600.0))
    return aider_bin, timeout


def run_aider_subprocess(rel_path: str, edit_message: str) -> Tuple[int, str, str]:
    aider_bin, timeout = _aider_bin_and_timeout()
    # aider CLI：--yes-always（等价于环境变量 AIDER_YES_ALWAYS）
    aider_cmd = [
        aider_bin,
        "--yes-always",
        "--message",
        edit_message,
        rel_path,
    ]
    result = subprocess.run(
        aider_cmd,
        cwd=str(_PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return int(result.returncode), result.stdout or "", result.stderr or ""


def run_aider_ask_subprocess(question: str) -> Tuple[int, str, str]:
    """aider 仅 --message，无文件参数（全局问答 / 排查）。"""
    aider_bin, timeout = _aider_bin_and_timeout()
    aider_cmd = [aider_bin, "--yes-always", "--message", question]
    result = subprocess.run(
        aider_cmd,
        cwd=str(_PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return int(result.returncode), result.stdout or "", result.stderr or ""


def cedarstar_err_log_path() -> str:
    """与 supervisord 中 cedarstar 的 stderr_logfile 对齐，可通过 REPAIR_CEDARSTAR_ERR_LOG 覆盖。"""
    default = "/var/log/supervisor/cedarstar.err.log"
    return (os.getenv("REPAIR_CEDARSTAR_ERR_LOG") or default).strip() or default


def run_tail_err_log_subprocess() -> Tuple[int, str, str, str]:
    """
    tail -n 50 <日志文件>。返回 (returncode, stdout, stderr, log_path)。
    不使用 shell，路径仅来自配置。
    """
    log_path = cedarstar_err_log_path()
    result = subprocess.run(
        ["tail", "-n", "50", log_path],
        capture_output=True,
        text=True,
        timeout=max(5.0, _env_float("REPAIR_TAIL_TIMEOUT_SEC", 30.0)),
    )
    return int(result.returncode), result.stdout or "", result.stderr or "", log_path


def format_tail_log_report(rc: int, stdout: str, stderr: str, log_path: str) -> str:
    """合并输出；总长超过 4000 字符时仅保留整个字符串的最后 4000 个字符。"""
    parts = [
        "【最近 50 行 stderr 日志】",
        f"path={log_path}",
        f"returncode={rc}",
        "",
        "— stdout —",
        (stdout or "").rstrip() or "（空）",
    ]
    if (stderr or "").strip():
        parts.extend(["", "— stderr —", stderr.rstrip()])
    body = "\n".join(parts)
    max_chars = 4000
    if len(body) <= max_chars:
        return body
    return body[-max_chars:]


def run_supervisor_restart_subprocess() -> Tuple[int, str, str]:
    name = (os.getenv("REPAIR_SUPERVISOR_PROGRAM") or "cedarstar").strip() or "cedarstar"
    timeout = max(10.0, _env_float("REPAIR_SUPERVISORCTL_TIMEOUT_SEC", 120.0))
    result = subprocess.run(
        ["supervisorctl", "restart", name],
        cwd=str(_PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return int(result.returncode), result.stdout or "", result.stderr or ""


def format_aider_report(rc: int, stdout: str, stderr: str) -> str:
    body = "\n".join(
        [
            "【Aider 执行结果】",
            f"returncode={rc}",
            "— stdout —",
            (stdout or "").strip() or "（空）",
            "— stderr —",
            (stderr or "").strip() or "（空）",
        ]
    )
    return _truncate_for_telegram(body)


def format_supervisor_report(rc: int, stdout: str, stderr: str) -> str:
    body = "\n".join(
        [
            "【supervisorctl restart 结果】",
            f"returncode={rc}",
            "— stdout —",
            (stdout or "").strip() or "（空）",
            "— stderr —",
            (stderr or "").strip() or "（空）",
        ]
    )
    return _truncate_for_telegram(body)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "CedarStar repair bot\n\n"
        "/edit <相对路径> <说明> — aider 改指定文件，成功后 supervisorctl restart\n"
        "/ask <问题> — aider 仅带问题、扫描/排查项目，不重启\n"
        "/log — tail -n 50 服务 stderr 日志（路径见 REPAIR_CEDARSTAR_ERR_LOG）\n\n"
        "项目根：" + str(_PROJECT_ROOT)
    )


async def edit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg:
        return
    chat_id = msg.chat.id if msg.chat else None
    if not _is_chat_allowed(chat_id):
        await _reply_unauthorized(msg)
        return

    try:
        try:
            raw_target, instruction = parse_edit_command_from_args(list(context.args or []))
            rel_path = resolve_safe_relative_path(raw_target)
        except ValueError as e:
            await msg.reply_text(str(e))
            return

        await _before_aider_subprocess(context, msg, edit_mode=True)

        try:
            rc, out, err = await asyncio.to_thread(run_aider_subprocess, rel_path, instruction)
        except subprocess.TimeoutExpired:
            logger.warning("Aider /edit subprocess.TimeoutExpired")
            try:
                await msg.reply_text(_MSG_AIDER_TIMEOUT)
            except Exception as e2:
                logger.warning("发送超时提示失败: %s", _exc_detail(e2))
            return
        except Exception as e:
            logger.exception("Aider 调用异常: %s", _exc_detail(e))
            await msg.reply_text("❌ 调用 Aider 时发生异常:\n" + _exc_detail(e)[:3500])
            return

        aider_report = format_aider_report(rc, out, err)
        if rc != 0:
            try:
                await msg.reply_text("❌ Aider 未成功完成（非零退出码）。\n\n" + aider_report)
            except Exception as e:
                logger.warning("发送 Aider 失败报告出错: %s", _exc_detail(e))
            return

        try:
            await msg.reply_text("✅ Aider 已成功结束。\n\n" + aider_report)
        except Exception as e:
            logger.warning("发送 Aider 成功报告出错: %s", _exc_detail(e))

        await _before_supervisor_restart(context, msg)

        try:
            rc_s, out_s, err_s = await asyncio.to_thread(run_supervisor_restart_subprocess)
        except subprocess.TimeoutExpired as e:
            await msg.reply_text(
                "⚠️ Aider 已成功，但 supervisorctl 执行超时。\n" + _exc_detail(e)[:3500]
            )
            return
        except Exception as e:
            logger.exception("supervisorctl 异常: %s", _exc_detail(e))
            await msg.reply_text(
                "⚠️ Aider 已成功，但执行 supervisorctl restart 时异常:\n"
                + _exc_detail(e)[:3500]
            )
            return

        sup_report = format_supervisor_report(rc_s, out_s, err_s)
        prefix = (
            "✅ 已执行 supervisorctl restart。\n\n"
            if rc_s == 0
            else "❌ supervisorctl restart 返回非零。\n\n"
        )
        try:
            await msg.reply_text(prefix + sup_report)
        except Exception as e:
            logger.warning("发送 supervisor 报告出错: %s", _exc_detail(e))

    except TelegramNetworkError as e:
        logger.warning("Telegram 网络错误 chat_id=%s: %s", chat_id, _exc_detail(e))
    except Exception as e:
        logger.exception("edit 未预期错误:\n%s", traceback.format_exc())
        try:
            await msg.reply_text("❌ /edit 处理过程中发生未预期错误:\n" + _exc_detail(e)[:3500])
        except Exception as e2:
            logger.warning("无法向用户发送错误说明: %s", _exc_detail(e2))


async def ask_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg:
        return
    chat_id = msg.chat.id if msg.chat else None
    if not _is_chat_allowed(chat_id):
        await _reply_unauthorized(msg)
        return

    try:
        try:
            question = parse_ask_from_args(list(context.args or []))
        except ValueError as e:
            await msg.reply_text(str(e))
            return

        await _before_aider_subprocess(context, msg, edit_mode=False)

        try:
            rc, out, err = await asyncio.to_thread(run_aider_ask_subprocess, question)
        except subprocess.TimeoutExpired:
            logger.warning("Aider /ask subprocess.TimeoutExpired")
            try:
                await msg.reply_text(_MSG_AIDER_TIMEOUT)
            except Exception as e2:
                logger.warning("ask: 发送超时提示失败: %s", _exc_detail(e2))
            return
        except Exception as e:
            logger.exception("ask: Aider 调用异常: %s", _exc_detail(e))
            await msg.reply_text("❌ 调用 Aider 时发生异常:\n" + _exc_detail(e)[:3500])
            return

        report = format_aider_report(rc, out, err)
        prefix = "✅ Aider 已结束（/ask，未重启服务）。\n\n" if rc == 0 else "❌ Aider 已结束（非零退出码）。\n\n"
        try:
            await msg.reply_text(prefix + report)
        except Exception as e:
            logger.warning("ask: 发送报告失败: %s", _exc_detail(e))

    except TelegramNetworkError as e:
        logger.warning("ask: Telegram 网络错误 chat_id=%s: %s", chat_id, _exc_detail(e))
    except Exception as e:
        logger.exception("ask 未预期错误:\n%s", traceback.format_exc())
        try:
            await msg.reply_text("❌ /ask 处理过程中发生未预期错误:\n" + _exc_detail(e)[:3500])
        except Exception as e2:
            logger.warning("ask: 无法发送错误说明: %s", _exc_detail(e2))


async def log_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg:
        return
    chat_id = msg.chat.id if msg.chat else None
    if not _is_chat_allowed(chat_id):
        await _reply_unauthorized(msg)
        return

    if context.args:
        try:
            await msg.reply_text("用法：/log（无需参数）")
        except Exception:
            pass
        return

    try:
        await _before_tail_log(context, msg)
        try:
            await msg.reply_text("⏳ 正在读取日志…")
        except Exception as e:
            logger.warning("log: 无法发送等待提示: %s", _exc_detail(e))

        try:
            rc, out, err, log_path = await asyncio.to_thread(run_tail_err_log_subprocess)
        except subprocess.TimeoutExpired as e:
            await msg.reply_text(
                "❌ tail 执行超时（可调大 REPAIR_TAIL_TIMEOUT_SEC）。\n" + _exc_detail(e)
            )
            return
        except Exception as e:
            logger.exception("log: tail 异常: %s", _exc_detail(e))
            await msg.reply_text("❌ 读取日志时发生异常:\n" + _exc_detail(e)[:3500])
            return

        report = format_tail_log_report(rc, out, err, log_path)
        prefix = "✅ " if rc == 0 else "⚠️ tail 返回非零（文件不存在或无权限时常见）。\n"
        try:
            await msg.reply_text(prefix + report)
        except Exception as e:
            logger.warning("log: 发送报告失败: %s", _exc_detail(e))

    except TelegramNetworkError as e:
        logger.warning("log: Telegram 网络错误 chat_id=%s: %s", chat_id, _exc_detail(e))
    except Exception as e:
        logger.exception("log 未预期错误:\n%s", traceback.format_exc())
        try:
            await msg.reply_text("❌ /log 处理过程中发生未预期错误:\n" + _exc_detail(e)[:3500])
        except Exception as e2:
            logger.warning("log: 无法发送错误说明: %s", _exc_detail(e2))


def main() -> None:
    load_dotenv(_PROJECT_ROOT / ".env")
    token = _repair_token()
    if not token:
        logger.error("请设置 REPAIR_TELEGRAM_BOT_TOKEN 或 TELEGRAM_BOT_TOKEN")
        sys.exit(1)

    proxy = _repair_proxy()

    def _http_request() -> HTTPXRequest:
        return HTTPXRequest(
            connect_timeout=25.0,
            read_timeout=120.0,
            write_timeout=120.0,
            proxy=proxy,
            httpx_kwargs={"trust_env": False},
        )

    app = (
        Application.builder()
        .token(token)
        .request(_http_request())
        .get_updates_request(_http_request())
        .build()
    )
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("edit", edit_command))
    app.add_handler(CommandHandler("ask", ask_command))
    app.add_handler(CommandHandler("log", log_command))

    logger.info("repair_bot 启动 polling，项目根: %s", _PROJECT_ROOT)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
