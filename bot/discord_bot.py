"""
Discord 机器人模块。

接收 Discord 消息，调用 LLM 接口生成回复，并发送回 Discord。
"""

import os
import sys
import asyncio
import base64
import logging
import traceback
import requests
from typing import Any, Dict, List, Optional

# 添加当前目录到 Python 路径，确保可以导入本地模块
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import discord
from discord.ext import commands

from config import config, validate_config, Platform
from llm.llm_interface import (
    LLMInterface,
    complete_with_lutopia_tool_loop,
)
from bot.message_buffer import MessageBuffer, ordered_media_type_from_buffer
from bot.reply_citations import schedule_update_memory_hits_and_clean_reply
from bot.stt_client import transcribe_voice
from bot.vision_caption import schedule_generate_image_caption
from memory.database import save_message
from memory.micro_batch import trigger_micro_batch_check
from memory.context_builder import build_context
from tools.lutopia import strip_lutopia_user_facing_assistant_text


# 设置日志
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class DiscordBot:
    """
    Discord 机器人类。
    
    处理 Discord 事件，调用 LLM 生成回复。
    """
    
    def __init__(self):
        """
        初始化 Discord 机器人。
        """
        # 创建 Discord 客户端
        intents = discord.Intents.default()
        intents.message_content = True
        intents.messages = True
        
        # 配置代理
        proxy_config = self._get_proxy_config()
        
        self.bot = commands.Bot(
            command_prefix="!",
            intents=intents,
            help_command=None,  # 禁用默认帮助命令
            proxy=proxy_config.get('http') if proxy_config else None
        )
        
        # 注意：LLMInterface 不在这里固化，而是每次请求时动态创建，以支持热更新
        # 存储对话历史（按用户ID和频道ID）
        self.conversation_histories = {}
        
        self._message_buffer = MessageBuffer(
            flush_callback=self._flush_buffered_messages,
            log=logger,
        )
        
        # 设置事件处理器
        self._setup_handlers()
        
        logger.info("Discord 机器人初始化完成")
        if proxy_config:
            logger.info(f"代理配置: {proxy_config}")
    
    def _get_proxy_config(self):
        """
        获取代理配置。
        
        Returns:
            dict: 代理配置字典，如果未启用代理则返回 None
        """
        return config.proxy_dict
    
    def _setup_handlers(self):
        """
        设置 Discord 事件处理器。
        """
        @self.bot.event
        async def on_ready():
            """机器人准备就绪时触发。"""
            logger.info(f"机器人已登录为: {self.bot.user.name}")
            logger.info(f"机器人 ID: {self.bot.user.id}")
            logger.info(f"已加入 {len(self.bot.guilds)} 个服务器")
            
            # 通知 dashboard 模块：Discord 已上线
            try:
                from api.dashboard import set_bot_online
                set_bot_online("discord", True)
                logger.info("已更新 Discord 在线状态 → True")
            except Exception as e:
                logger.warning(f"更新 Discord 在线状态失败: {e}")
            
            # 设置机器人状态
            activity = discord.Game(name="与用户聊天")
            await self.bot.change_presence(activity=activity)

        @self.bot.event
        async def on_disconnect():
            """机器人断开连接时触发。"""
            try:
                from api.dashboard import set_bot_online
                set_bot_online("discord", False)
                logger.info("已更新 Discord 在线状态 → False")
            except Exception as e:
                logger.warning(f"更新 Discord 离线状态失败: {e}")
        
        @self.bot.event
        async def on_message(message):
            """
            收到消息时触发。
            
            使用消息缓冲逻辑：收到消息后等待 buffer_delay 配置的时间（默认5秒），
            期间如果同一 session 有新消息进来就重置计时器，
            超时后才将缓冲区内所有消息合并成一条处理。
            
            Args:
                message: Discord 消息对象
            """
            # 忽略机器人自己的消息
            if message.author == self.bot.user:
                return
            
            # 检查是否提及机器人或是在私聊中
            is_mentioned = self.bot.user in message.mentions
            is_dm = isinstance(message.channel, discord.DMChannel)
            
            # 如果消息提及机器人或是在私聊中，则将消息加入缓冲区
            if is_mentioned or is_dm:
                has_text = bool(message.clean_content and message.clean_content.strip())
                has_img = any(
                    a.content_type and str(a.content_type).lower().startswith("image/")
                    for a in message.attachments
                )
                has_audio = any(
                    str(a.content_type or "").lower() in ("audio/ogg", "audio/mpeg")
                    for a in message.attachments
                )
                if has_text or has_img or has_audio:
                    await self._add_to_buffer(message)
            
            # 处理命令
            await self.bot.process_commands(message)
        
        @self.bot.command(name="ping")
        async def ping_command(ctx):
            """
            测试机器人是否在线的命令。
            
            Args:
                ctx: 命令上下文
            """
            latency = round(self.bot.latency * 1000)
            await ctx.send(f"🏓 Pong! 延迟: {latency}ms")
        
        @self.bot.command(name="clear")
        async def clear_command(ctx):
            """
            清除当前对话历史的命令。
            
            Args:
                ctx: 命令上下文
            """
            key = self._get_conversation_key(ctx.author.id, ctx.channel.id)
            if key in self.conversation_histories:
                del self.conversation_histories[key]
                await ctx.send("✅ 对话历史已清除")
            else:
                await ctx.send("ℹ️ 当前没有对话历史")
        
        @self.bot.command(name="model")
        async def model_command(ctx):
            """
            显示当前使用的模型信息的命令。
            
            Args:
                ctx: 命令上下文
            """
            # 动态创建以读取最新激活配置
            current_llm = await LLMInterface.create()
            model_info = (
                f"🤖 **当前模型**: {current_llm.model_name}\n"
                f"📊 **最大 token**: {current_llm.max_tokens}\n"
                f"🌡️ **温度**: {current_llm.temperature}\n"
                f"⏱️ **超时**: {current_llm.timeout}秒"
            )
            await ctx.send(model_info)
        
        @self.bot.command(name="help")
        async def help_command(ctx):
            """
            显示帮助信息的命令。
            
            Args:
                ctx: 命令上下文
            """
            help_text = (
                "**🤖 CedarStar Discord 机器人帮助**\n\n"
                "**基本用法**:\n"
                "- 在消息中提及我，我会回复你\n"
                "- 私聊我直接对话\n\n"
                "**可用命令**:\n"
                "`!ping` - 测试机器人是否在线\n"
                "`!clear` - 清除当前对话历史\n"
                "`!model` - 显示当前模型信息\n"
                "`!help` - 显示此帮助信息\n\n"
                "**注意事项**:\n"
                "- 对话历史会保存在内存中，直到清除或机器人重启\n"
                "- 回复可能因模型配置而有所不同"
            )
            await ctx.send(help_text)
    
    def _get_conversation_key(self, user_id: int, channel_id: int) -> str:
        """
        获取对话历史的键。
        
        Args:
            user_id: 用户ID
            channel_id: 频道ID
            
        Returns:
            str: 对话历史键
        """
        return f"{user_id}_{channel_id}"
    
    def _split_message(self, message: str, max_length: int = 2000) -> list:
        """
        将长消息分割成多个部分。
        
        Args:
            message: 原始消息
            max_length: 每个部分的最大长度
            
        Returns:
            list: 分割后的消息列表
        """
        if len(message) <= max_length:
            return [message]
        
        parts = []
        while message:
            if len(message) <= max_length:
                parts.append(message)
                break
            
            # 查找最后一个换行符或句号作为分割点
            split_index = max_length
            for i in range(max_length - 1, max_length - 100, -1):
                if i < len(message) and message[i] in ('\n', '。', '.', '!', '?'):
                    split_index = i + 1
                    break
            
            parts.append(message[:split_index])
            message = message[split_index:]
        
        return parts
    
    MAX_IMAGE_BYTES = 10 * 1024 * 1024
    MAX_VOICE_DOWNLOAD_BYTES = 50 * 1024 * 1024
    WHISPER_MAX_VOICE_BYTES = 25 * 1024 * 1024

    async def _add_to_buffer(self, message: discord.Message):
        """
        将消息添加到缓冲区，并启动/重置缓冲定时器。
        
        Args:
            message: Discord 消息对象
        """
        session_id = f"{message.author.id}_{message.channel.id}"
        self._message_buffer.begin_heavy(session_id)
        try:
            content = (message.clean_content or "").strip()
            text_parts: List[str] = []
            if content:
                text_parts.append(content)
            image_payloads: List[Dict[str, Any]] = []
            from_voice = False
            for att in message.attachments:
                ct = (att.content_type or "").lower()
                if not ct.startswith("image/"):
                    continue
                if att.size and att.size > self.MAX_IMAGE_BYTES:
                    text_parts.append("[发送了1张图片（文件过大，已跳过视觉解析）]")
                    continue
                try:
                    raw = await att.read()
                    b64 = base64.b64encode(raw).decode("ascii")
                    image_payloads.append(
                        {
                            "type": "image",
                            "data": b64,
                            "caption": "",
                            "mime_type": att.content_type or "image/jpeg",
                        }
                    )
                except Exception as e:
                    logger.error("读取 Discord 图片附件失败: %s", e)
                    text_parts.append("[发送了1张图片（文件过大，已跳过视觉解析）]")
            oversized_v = "[语音] 文件过大，跳过转录"
            fail_v = "[语音] 转录失败"
            for att in message.attachments:
                ct = (att.content_type or "").lower()
                if ct not in ("audio/ogg", "audio/mpeg"):
                    continue
                from_voice = True
                if att.size and att.size > self.MAX_VOICE_DOWNLOAD_BYTES:
                    text_parts.append(oversized_v)
                    continue
                try:
                    raw = await att.read()
                except Exception as e:
                    logger.error("读取 Discord 语音附件失败: %s", e)
                    text_parts.append(fail_v)
                    continue
                if len(raw) > self.WHISPER_MAX_VOICE_BYTES:
                    text_parts.append(oversized_v)
                    continue
                try:
                    mime = att.content_type or (
                        "audio/ogg" if ct == "audio/ogg" else "audio/mpeg"
                    )
                    t = await transcribe_voice(raw, mime_type=mime)
                    text_parts.append(f"[语音] {t}")
                except Exception as e:
                    logger.warning("Discord 语音转录失败: %s", e)
                    text_parts.append(fail_v)
            merged_text = "\n".join(text_parts).strip()
            await self._message_buffer.add_to_buffer(
                session_id,
                {
                    "message": message,
                    "content": merged_text,
                    "image_payloads": image_payloads,
                    "from_voice": from_voice,
                    "timestamp": asyncio.get_event_loop().time(),
                },
            )
        finally:
            self._message_buffer.end_heavy(session_id)

    async def _flush_buffered_messages(
        self,
        session_id: str,
        combined_raw: str,
        combined_content: str,
        images: List[Dict[str, Any]],
        buffer_messages: List[Dict[str, Any]],
        text_for_llm: str,
    ) -> None:
        """缓冲到期后由 MessageBuffer 调用：Discord 侧打字、生成、分片发送。"""
        base_message = buffer_messages[0]["message"]
        async with base_message.channel.typing():
            reply = await self._generate_reply_from_buffer(
                base_message,
                combined_raw,
                combined_content,
                session_id,
                buffer_messages=buffer_messages,
                images=images,
                text_for_llm=text_for_llm,
            )
            if reply:
                if len(reply) > 2000:
                    chunks = self._split_message(reply)
                    for chunk in chunks:
                        stack = "".join(traceback.format_stack())
                        logging.warning(f"send called from: {stack}")
                        await base_message.channel.send(chunk)
                        await asyncio.sleep(0.5)  # 避免速率限制
                else:
                    stack = "".join(traceback.format_stack())
                    logging.warning(f"send called from: {stack}")
                    await base_message.channel.send(reply)
    
    async def _generate_reply_from_buffer(
        self,
        base_message: discord.Message,
        combined_raw: str,
        combined_content: str,
        session_id: str,
        buffer_messages: List[Dict[str, Any]],
        images: Optional[List[Dict[str, Any]]] = None,
        text_for_llm: Optional[str] = None,
    ) -> Optional[str]:
        """
        从缓冲区合并的消息生成回复。
        
        Args:
            base_message: 基础消息对象（第一条消息）
            combined_raw: 落库用原始内容（不含引用前缀）
            combined_content: LLM 用内容（可能含引用前缀）
            session_id: 会话ID
            buffer_messages: 本缓冲批次原始条目（用于 from_voice 等标记）
            images: 当前轮图片 payload
            text_for_llm: 多模态请求用纯文本
            
        Returns:
            Optional[str]: 生成的回复，如果生成失败则返回 None
        """
        try:
            # 每次动态创建 LLMInterface，以读取最新激活配置（支持热更新）
            llm = await LLMInterface.create()
            oral = (
                bool(getattr(llm, "enable_lutopia", False))
                or bool(getattr(llm, "enable_weather_tool", False))
                or bool(getattr(llm, "enable_weibo_tool", False))
                or bool(getattr(llm, "enable_search_tool", False))
            ) and not llm._use_anthropic_messages_api()
            # 使用 context builder 构建完整的对话上下文
            context = await build_context(
                session_id,
                combined_content,
                images=images or None,
                llm_user_text=text_for_llm or None,
                tool_oral_coaching=oral,
                exclude_message_id=user_row_id if 'user_row_id' in locals() else None,
            )
            
            # 提取 system prompt 和 messages
            system_prompt = context.get("system_prompt", "")
            messages = context.get("messages", [])
            
            # 如果没有构建出有效的 messages，使用最小化版本
            if not messages:
                messages = [{"role": "user", "content": combined_content}]
            
            lutopia_appendix = ""
            if oral:
                outcome = await complete_with_lutopia_tool_loop(
                    llm,
                    messages,
                    platform=Platform.DISCORD,
                    session_id=session_id,
                    user_message_id=user_row_id if 'user_row_id' in locals() else None,
                )
                llm_resp = outcome.response
                reply_display = schedule_update_memory_hits_and_clean_reply(
                    outcome.aggregated_assistant_text
                )
                reply = reply_display
                lutopia_appendix = outcome.behavior_appendix or ""
            else:
                llm_resp = llm.generate_with_context_and_tracking(
                    messages, platform=Platform.DISCORD
                )
                reply = schedule_update_memory_hits_and_clean_reply(llm_resp.content)
                reply_display = reply
            
            # 保存用户消息到数据库（使用原始内容，不含引用前缀）
            has_img = bool(images)
            media_t = ordered_media_type_from_buffer(buffer_messages)
            user_row_id = await save_message(
                session_id=session_id,
                role="user",
                content=combined_raw,
                user_id=str(base_message.author.id),
                channel_id=str(base_message.channel.id),
                message_id=str(base_message.id),
                character_id=llm.character_id,
                platform=Platform.DISCORD,
                media_type=media_t,
                image_caption=None,
                vision_processed=0 if has_img else 1,
            )
            if has_img and user_row_id:
                schedule_generate_image_caption(
                    user_row_id,
                    images,
                    (text_for_llm or "").strip(),
                    platform=Platform.DISCORD,
                )
            
            assistant_db = reply_display
            if lutopia_appendix:
                assistant_db = (
                    (reply_display.rstrip() + "\n" + lutopia_appendix)
                    if (reply_display or "").strip()
                    else lutopia_appendix
                )
            await save_message(
                session_id=session_id,
                role="assistant",
                content=assistant_db,
                user_id=str(base_message.author.id),
                channel_id=str(base_message.channel.id),
                message_id=f"ai_{base_message.id}",
                character_id=llm.character_id,
                platform=Platform.DISCORD,
                thinking=llm_resp.thinking
            )
            
            logger.info(f"为缓冲区生成回复: session_id={session_id}, context 消息数量={len(messages)}")
            logger.debug(f"System prompt 长度: {len(system_prompt)}")
            
            # 异步触发微批处理检查
            asyncio.create_task(trigger_micro_batch_check(session_id))
            
            return strip_lutopia_user_facing_assistant_text(reply_display)
            
        except ValueError as e:
            logger.error(f"LLM 配置错误: {e}")
            return "抱歉，LLM 配置有问题，请检查 API 密钥设置。"
        except requests.exceptions.Timeout:
            if images:
                logger.error(
                    "LLM 请求超时（本轮含图片/多模态，可调 LLM_VISION_TIMEOUT / LLM_TIMEOUT）"
                )
                return (
                    "抱歉，模型响应超时。带图请求更慢；可在 .env 提高 LLM_VISION_TIMEOUT（默认 180 秒）或 LLM_TIMEOUT。"
                )
            logger.error(
                "LLM 请求超时（主对话无多模态图片 payload；上下文长或上游慢时可调 LLM_TIMEOUT，默认 60 秒）"
            )
            return (
                "抱歉，模型响应超时。若上下文很长或接口较慢，"
                "请在 .env 提高 LLM_TIMEOUT（默认 60 秒）。"
            )
        except Exception as e:
            logger.error(f"生成回复时出错: {e}")
            logger.exception(e)  # 记录完整异常堆栈
            return "抱歉，生成回复时出错了，请稍后再试。"
    
    async def _generate_reply(self, message: discord.Message) -> Optional[str]:
        """
        生成回复消息。
        
        使用新的 context builder 构建完整的对话上下文。
        
        Args:
            message: Discord 消息对象
            
        Returns:
            Optional[str]: 生成的回复，如果生成失败则返回 None
        """
        try:
            # 创建会话ID（用户ID + 频道ID）
            session_id = f"{message.author.id}_{message.channel.id}"
            
            # 清理消息内容（移除提及）
            content = message.clean_content
            
            # 每次动态创建 LLMInterface，以读取最新激活配置（支持热更新）
            llm = await LLMInterface.create()
            oral = (
                bool(getattr(llm, "enable_lutopia", False))
                or bool(getattr(llm, "enable_weather_tool", False))
                or bool(getattr(llm, "enable_weibo_tool", False))
                or bool(getattr(llm, "enable_search_tool", False))
            ) and not llm._use_anthropic_messages_api()
            # 使用 context builder 构建完整的对话上下文
            context = await build_context(
                session_id, 
                content, 
                tool_oral_coaching=oral,
                exclude_message_id=user_row_id if 'user_row_id' in locals() else None,
            )
            
            # 提取 system prompt 和 messages
            system_prompt = context.get("system_prompt", "")
            messages = context.get("messages", [])
            
            # 如果没有构建出有效的 messages，使用最小化版本
            if not messages:
                messages = [{"role": "user", "content": content}]
            
            lutopia_appendix = ""
            if oral:
                outcome = await complete_with_lutopia_tool_loop(
                    llm,
                    messages,
                    platform=Platform.DISCORD,
                    session_id=session_id,
                    user_message_id=user_row_id if 'user_row_id' in locals() else None,
                )
                llm_resp = outcome.response
                reply_display = schedule_update_memory_hits_and_clean_reply(
                    outcome.aggregated_assistant_text
                )
                reply = reply_display
                lutopia_appendix = outcome.behavior_appendix or ""
            else:
                llm_resp = llm.generate_with_context_and_tracking(
                    messages, platform=Platform.DISCORD
                )
                reply = schedule_update_memory_hits_and_clean_reply(llm_resp.content)
                reply_display = reply
            
            # 保存用户消息到数据库
            await save_message(
                session_id=session_id,
                role="user",
                content=content,
                user_id=str(message.author.id),
                channel_id=str(message.channel.id),
                message_id=str(message.id),
                character_id=llm.character_id,
                platform=Platform.DISCORD
            )
            
            assistant_db = reply_display
            if lutopia_appendix:
                assistant_db = (
                    (reply_display.rstrip() + "\n" + lutopia_appendix)
                    if (reply_display or "").strip()
                    else lutopia_appendix
                )
            await save_message(
                session_id=session_id,
                role="assistant",
                content=assistant_db,
                user_id=str(message.author.id),
                channel_id=str(message.channel.id),
                message_id=f"ai_{message.id}",
                character_id=llm.character_id,
                platform=Platform.DISCORD,
                thinking=llm_resp.thinking
            )
            
            logger.info(f"为用户 {message.author.name} 生成回复，context 消息数量: {len(messages)}")
            logger.debug(f"System prompt 长度: {len(system_prompt)}")
            
            # 异步触发微批处理检查
            asyncio.create_task(trigger_micro_batch_check(session_id))
            
            return strip_lutopia_user_facing_assistant_text(reply_display)
            
        except ValueError as e:
            logger.error(f"LLM 配置错误: {e}")
            return "抱歉，LLM 配置有问题，请检查 API 密钥设置。"
        except requests.exceptions.Timeout:
            logger.error("LLM 请求超时（可调 LLM_TIMEOUT，默认 60 秒）")
            return (
                "抱歉，模型响应超时。可在 .env 提高 LLM_TIMEOUT（默认 60 秒）。"
            )
        except Exception as e:
            logger.error(f"生成回复时出错: {e}")
            logger.exception(e)  # 记录完整异常堆栈
            return "抱歉，生成回复时出错了，请稍后再试。"
    
    def run(self):
        """
        运行 Discord 机器人。
        
        Raises:
            ValueError: 如果 Discord 令牌未设置
        """
        try:
            token = config.DISCORD_BOT_TOKEN
            if not token or token == "<your_token_here>":
                raise ValueError("DISCORD_BOT_TOKEN 未在 .env 文件中正确设置")
            
            logger.info("启动 Discord 机器人...")
            
            # 设置环境变量以便 aiohttp 使用代理
            proxy_config = self._get_proxy_config()
            if proxy_config:
                # 设置环境变量
                if 'http' in proxy_config:
                    os.environ['HTTP_PROXY'] = proxy_config['http']
                if 'https' in proxy_config:
                    os.environ['HTTPS_PROXY'] = proxy_config['https']
                
                logger.info(f"已设置代理环境变量: HTTP_PROXY={proxy_config.get('http')}, HTTPS_PROXY={proxy_config.get('https')}")
            
            self.bot.run(token)
            
        except discord.LoginFailure:
            logger.error("Discord 登录失败，请检查令牌是否正确")
            raise
        except Exception as e:
            logger.error(f"启动 Discord 机器人时出错: {e}")
            raise


def main():
    """
    Discord 机器人主函数。
    """
    try:
        # 验证配置
        validate_config()
        
        # 创建并运行机器人
        bot = DiscordBot()
        bot.run()
        
    except ValueError as e:
        logger.error(f"配置验证失败: {e}")
        print(f"错误: {e}")
        print("请检查 .env 文件中的配置项")
    except Exception as e:
        logger.error(f"机器人运行失败: {e}")
        print(f"错误: {e}")


if __name__ == "__main__":
    """Discord 机器人模块测试入口。"""
    main()
