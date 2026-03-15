"""
Telegram 机器人模块。

接收 Telegram 消息，调用 LLM 接口生成回复，并发送回 Telegram。
复用现有的消息缓冲逻辑，与 Discord 实现解耦。
"""

import os
import sys
import asyncio
import logging
from typing import Optional

# 添加当前目录到 Python 路径，确保可以导入本地模块
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

from config import config, validate_config
from llm.llm_interface import LLMInterface
from memory.database import save_message, get_database
from memory.micro_batch import trigger_micro_batch_check
from memory.context_builder import build_context


# 设置日志
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class TelegramBot:
    """
    Telegram 机器人类。
    
    处理 Telegram 事件，调用 LLM 生成回复。
    复用现有的消息缓冲逻辑，与平台对象解耦。
    """
    
    def __init__(self):
        """
        初始化 Telegram 机器人。
        """
        # 注意：LLMInterface 不在这里固化，而是每次请求时动态创建，以支持热更新
        
        # 消息缓冲区：key 为 session_id，value 为消息列表
        self.message_buffers = {}
        # 缓冲定时器：key 为 session_id，value 为 asyncio.Task
        self.buffer_timers = {}
        # 缓冲锁：防止并发问题
        self.buffer_locks = {}
        
        logger.info("Telegram 机器人初始化完成")
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        处理 /start 命令。
        
        Args:
            update: Telegram 更新对象
            context: 上下文对象
        """
        welcome_text = (
            "🤖 欢迎使用 CedarStar AI 助手！\n\n"
            "我是一个智能助手，可以与你进行对话。\n"
            "直接发送消息即可开始聊天。\n\n"
            "可用命令：\n"
            "/start - 显示此欢迎信息\n"
            "/help - 显示帮助信息\n"
            "/model - 显示当前模型信息\n"
            "/clear - 清除当前对话历史\n"
        )
        await update.message.reply_text(welcome_text)
    
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        处理 /help 命令。
        
        Args:
            update: Telegram 更新对象
            context: 上下文对象
        """
        help_text = (
            "🤖 CedarStar Telegram 机器人帮助\n\n"
            "基本用法：\n"
            "- 直接发送消息，我会回复你\n\n"
            "可用命令：\n"
            "/start - 显示欢迎信息\n"
            "/help - 显示此帮助信息\n"
            "/model - 显示当前模型信息\n"
            "/clear - 清除当前对话历史\n\n"
            "注意事项：\n"
            "- 对话历史会保存在数据库中\n"
            "- 回复可能因模型配置而有所不同"
        )
        await update.message.reply_text(help_text)
    
    async def model_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        处理 /model 命令。
        
        Args:
            update: Telegram 更新对象
            context: 上下文对象
        """
        # 动态创建以读取最新激活配置
        current_llm = LLMInterface()
        model_info = (
            f"🤖 当前模型: {current_llm.model_name}\n"
            f"📊 最大 token: {current_llm.max_tokens}\n"
            f"🌡️ 温度: {current_llm.temperature}\n"
            f"⏱️ 超时: {current_llm.timeout}秒"
        )
        await update.message.reply_text(model_info)
    
    async def clear_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        处理 /clear 命令。
        
        Args:
            update: Telegram 更新对象
            context: 上下文对象
        """
        # 获取会话ID
        chat_id = update.effective_chat.id
        session_id = f"telegram_{chat_id}"
        
        # 清除对话历史（在数据库中标记为已摘要）
        from memory.database import get_database
        db = get_database()
        db.clear_session_messages(session_id)
        
        await update.message.reply_text("✅ 对话历史已清除")
    
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        处理文本消息。
        
        使用消息缓冲逻辑：收到消息后等待 buffer_delay 配置的时间（默认5秒），
        期间如果同一 session 有新消息进来就重置计时器，
        超时后才将缓冲区内所有消息合并成一条处理。
        
        Args:
            update: Telegram 更新对象
            context: 上下文对象
        """
        # 忽略非文本消息
        if not update.message or not update.message.text:
            return
        
        # 获取消息信息
        message = update.message
        chat_id = message.chat.id
        user_id = message.from_user.id
        message_id = message.message_id
        content = message.text
        
        # 创建会话ID（格式：telegram_{chat_id}）
        session_id = f"telegram_{chat_id}"
        
        logger.info(f"收到 Telegram 消息: chat_id={chat_id}, user_id={user_id}, 内容长度={len(content)}")
        
        # 将消息添加到缓冲区
        await self._add_to_buffer(update, context, session_id, message, content, user_id, message_id)
    
    def _split_message(self, message: str, max_length: int = 4096) -> list:
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
    
    async def _add_to_buffer(self, update: Update, context: ContextTypes.DEFAULT_TYPE, 
                           session_id: str, message, content: str, user_id: str, message_id: str):
        """
        将消息添加到缓冲区，并启动/重置缓冲定时器。
        
        Args:
            update: Telegram 更新对象
            context: 上下文对象
            session_id: 会话ID
            message: 消息对象
            content: 消息内容
            user_id: 用户ID
            message_id: 消息ID
        """
        # 确保有锁对象
        if session_id not in self.buffer_locks:
            self.buffer_locks[session_id] = asyncio.Lock()
        
        async with self.buffer_locks[session_id]:
            # 将消息添加到缓冲区
            if session_id not in self.message_buffers:
                self.message_buffers[session_id] = []
            
            self.message_buffers[session_id].append({
                "update": update,
                "context": context,
                "message": message,
                "content": content,
                "user_id": user_id,
                "message_id": message_id,
                "timestamp": asyncio.get_event_loop().time()
            })
            
            logger.debug(f"消息添加到缓冲区: session_id={session_id}, 缓冲区大小={len(self.message_buffers[session_id])}")
            
            # 取消现有的定时器（如果有）
            if session_id in self.buffer_timers:
                self.buffer_timers[session_id].cancel()
                logger.debug(f"取消现有定时器: session_id={session_id}")
            
            # 启动新的定时器
            self.buffer_timers[session_id] = asyncio.create_task(
                self._process_buffer(session_id)
            )
    
    async def _process_buffer(self, session_id: str):
        """
        处理缓冲区中的消息。
        
        等待 buffer_delay 秒后，将缓冲区中的所有消息合并处理。
        
        Args:
            session_id: 会话ID
        """
        try:
            # 从数据库获取缓冲延迟时间
            db = get_database()
            buffer_delay_str = db.get_config("buffer_delay", "5")
            try:
                buffer_delay = int(buffer_delay_str)
            except ValueError:
                buffer_delay = 5  # 默认值
                
            logger.debug(f"开始缓冲等待: session_id={session_id}, 延迟={buffer_delay}秒")
            await asyncio.sleep(buffer_delay)
            
            # 确保有锁对象
            if session_id not in self.buffer_locks:
                self.buffer_locks[session_id] = asyncio.Lock()
            
            async with self.buffer_locks[session_id]:
                # 检查缓冲区是否还有消息
                if session_id not in self.message_buffers or not self.message_buffers[session_id]:
                    logger.debug(f"缓冲区为空，跳过处理: session_id={session_id}")
                    return
                
                # 获取缓冲区中的所有消息
                buffer_messages = self.message_buffers[session_id]
                
                # 清空缓冲区
                self.message_buffers[session_id] = []
                
                # 删除定时器
                if session_id in self.buffer_timers:
                    del self.buffer_timers[session_id]
                
                logger.info(f"处理缓冲区消息: session_id={session_id}, 消息数量={len(buffer_messages)}")
                
                # 合并所有消息内容
                combined_content = "\n".join([msg["content"] for msg in buffer_messages])
                
                # 使用第一条消息作为基础消息对象
                base_message = buffer_messages[0]["message"]
                base_update = buffer_messages[0]["update"]
                base_context = buffer_messages[0]["context"]
                base_user_id = buffer_messages[0]["user_id"]
                base_message_id = buffer_messages[0]["message_id"]
                
                # 显示"正在输入"状态
                await base_context.bot.send_chat_action(chat_id=base_message.chat.id, action="typing")
                
                # 生成回复
                reply = await self._generate_reply_from_buffer(
                    session_id=session_id,
                    combined_content=combined_content,
                    user_id=base_user_id,
                    chat_id=str(base_message.chat.id),
                    message_id=base_message_id,
                    base_message=base_message
                )
                
                # 发送回复
                if reply:
                    # 如果回复太长，分割成多个消息
                    if len(reply) > 4096:  # Telegram 消息长度限制
                        chunks = self._split_message(reply)
                        for chunk in chunks:
                            await base_message.reply_text(chunk)
                            await asyncio.sleep(0.5)  # 避免速率限制
                    else:
                        await base_message.reply_text(reply)
        
        except asyncio.CancelledError:
            logger.debug(f"缓冲定时器被取消: session_id={session_id}")
        except Exception as e:
            logger.error(f"处理缓冲区时出错: session_id={session_id}, 错误={e}")
            logger.exception(e)
    
    async def _generate_reply_from_buffer(self, session_id: str, combined_content: str, 
                                        user_id: str, chat_id: str, message_id: str, 
                                        base_message) -> Optional[str]:
        """
        从缓冲区合并的消息生成回复。
        
        Args:
            session_id: 会话ID
            combined_content: 合并后的消息内容
            user_id: 用户ID
            chat_id: 聊天ID
            message_id: 消息ID
            base_message: 基础消息对象（第一条消息）
            
        Returns:
            Optional[str]: 生成的回复，如果生成失败则返回 None
        """
        try:
            # 使用 context builder 构建完整的对话上下文
            context = build_context(session_id, combined_content)
            
            # 提取 system prompt 和 messages
            system_prompt = context.get("system_prompt", "")
            messages = context.get("messages", [])
            
            # 如果没有构建出有效的 messages，使用最小化版本
            if not messages:
                messages = [{"role": "user", "content": combined_content}]
            
            # 每次动态创建 LLMInterface，以读取最新激活配置（支持热更新）
            llm = LLMInterface()
            reply = llm.generate_with_context(messages)
            
            # 保存用户消息到数据库（合并后的消息）
            save_message(
                session_id=session_id,
                role="user",
                content=combined_content,
                user_id=user_id,
                channel_id=chat_id,
                message_id=message_id,
                character_id="sirius",
                platform="telegram"
            )
            
            # 保存AI回复到数据库
            save_message(
                session_id=session_id,
                role="assistant",
                content=reply,
                user_id=user_id,
                channel_id=chat_id,
                message_id=f"ai_{message_id}",
                character_id="sirius",
                platform="telegram"
            )
            
            logger.info(f"为缓冲区生成回复: session_id={session_id}, context 消息数量={len(messages)}")
            logger.debug(f"System prompt 长度: {len(system_prompt)}")
            
            # 异步触发微批处理检查
            asyncio.create_task(trigger_micro_batch_check(session_id))
            
            return reply
            
        except ValueError as e:
            logger.error(f"LLM 配置错误: {e}")
            return "抱歉，LLM 配置有问题，请检查 API 密钥设置。"
        except Exception as e:
            logger.error(f"生成回复时出错: {e}")
            logger.exception(e)  # 记录完整异常堆栈
            return "抱歉，生成回复时出错了，请稍后再试。"
    
    async def _generate_reply(self, session_id: str, content: str, 
                            user_id: str, chat_id: str, message_id: str) -> Optional[str]:
        """
        生成回复消息。
        
        复用现有的消息缓冲逻辑，与平台对象解耦。
        
        Args:
            session_id: 会话ID（格式：telegram_{chat_id}）
            content: 消息内容
            user_id: 用户ID
            chat_id: 聊天ID
            message_id: 消息ID
            
        Returns:
            Optional[str]: 生成的回复，如果生成失败则返回 None
        """
        try:
            # 使用 context builder 构建完整的对话上下文
            context = build_context(session_id, content)
            
            # 提取 system prompt 和 messages
            system_prompt = context.get("system_prompt", "")
            messages = context.get("messages", [])
            
            # 如果没有构建出有效的 messages，使用最小化版本
            if not messages:
                messages = [{"role": "user", "content": content}]
            
            # 每次动态创建 LLMInterface，以读取最新激活配置（支持热更新）
            llm = LLMInterface()
            reply = llm.generate_with_context(messages)
            
            # 保存用户消息到数据库
            save_message(
                session_id=session_id,
                role="user",
                content=content,
                user_id=user_id,
                channel_id=chat_id,
                message_id=message_id,
                character_id="sirius",
                platform="telegram"  # 添加 platform 字段
            )
            
            # 保存AI回复到数据库
            save_message(
                session_id=session_id,
                role="assistant",
                content=reply,
                user_id=user_id,
                channel_id=chat_id,
                message_id=f"ai_{message_id}",
                character_id="sirius",
                platform="telegram"  # 添加 platform 字段
            )
            
            logger.info(f"为 Telegram 用户 {user_id} 生成回复，context 消息数量: {len(messages)}")
            logger.debug(f"System prompt 长度: {len(system_prompt)}")
            
            # 异步触发微批处理检查
            asyncio.create_task(trigger_micro_batch_check(session_id))
            
            return reply
            
        except ValueError as e:
            logger.error(f"LLM 配置错误: {e}")
            return "抱歉，LLM 配置有问题，请检查 API 密钥设置。"
        except Exception as e:
            logger.error(f"生成回复时出错: {e}")
            logger.exception(e)  # 记录完整异常堆栈
            return "抱歉，生成回复时出错了，请稍后再试。"
    
    async def run_async(self):
        """
        异步运行 Telegram 机器人。
        
        使用 python-telegram-bot v20+ 的异步启动方式：
        app.initialize() + app.start() + app.updater.start_polling()
        确保不阻塞主事件循环。
        
        Raises:
            ValueError: 如果 Telegram 令牌未设置
        """
        try:
            token = config.TELEGRAM_BOT_TOKEN
            if not token:
                logger.warning("TELEGRAM_BOT_TOKEN 未设置，Telegram 机器人将不会启动")
                return
            
            logger.info("启动 Telegram 机器人...")
            
            # 创建 Application
            self.application = Application.builder().token(token).build()
            
            # 添加命令处理器
            self.application.add_handler(CommandHandler("start", self.start_command))
            self.application.add_handler(CommandHandler("help", self.help_command))
            self.application.add_handler(CommandHandler("model", self.model_command))
            self.application.add_handler(CommandHandler("clear", self.clear_command))
            
            # 添加消息处理器
            self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
            
            # 启动机器人（使用 polling 模式）
            logger.info("Telegram 机器人开始 polling...")
            await self.application.initialize()
            await self.application.start()
            await self.application.updater.start_polling()
            
            logger.info("Telegram 机器人已启动（异步 polling 模式）")
            
            # 通知 dashboard 模块：Telegram 已上线
            try:
                from api.dashboard import set_bot_online
                set_bot_online("telegram", True)
                logger.info("已更新 Telegram 在线状态 → True")
            except Exception as e:
                logger.warning(f"更新 Telegram 在线状态失败: {e}")
            
            # 保持运行直到收到停止信号
            stop_event = asyncio.Event()
            await stop_event.wait()
            
        except Exception as e:
            logger.error(f"启动 Telegram 机器人时出错: {e}")
            raise
    
    async def run(self):
        """
        运行 Telegram 机器人（兼容旧接口）。
        
        注意：这个方法会阻塞，建议使用 run_async()。
        """
        try:
            await self.run_async()
            # 保持运行
            await self.application.updater.idle()
        except Exception as e:
            logger.error(f"Telegram 机器人运行失败: {e}")
            raise
    
    async def stop(self):
        """
        停止 Telegram 机器人。
        """
        if hasattr(self, 'application'):
            await self.application.stop()
            await self.application.shutdown()
            logger.info("Telegram 机器人已停止")


async def run_telegram_bot():
    """
    运行 Telegram 机器人的便捷函数。
    
    Returns:
        asyncio.Task: Telegram 机器人任务
    """
    try:
        bot = TelegramBot()
        await bot.run()
    except Exception as e:
        logger.error(f"Telegram 机器人运行失败: {e}")
        raise


def main():
    """
    Telegram 机器人主函数。
    """
    try:
        # 验证配置
        validate_config()
        
        # 创建并运行机器人
        bot = TelegramBot()
        
        # 运行异步主循环
        asyncio.run(bot.run())
        
    except ValueError as e:
        logger.error(f"配置验证失败: {e}")
        print(f"错误: {e}")
        print("请检查 .env 文件中的配置项")
    except Exception as e:
        logger.error(f"机器人运行失败: {e}")
        print(f"错误: {e}")


if __name__ == "__main__":
    """Telegram 机器人模块测试入口。"""
    main()