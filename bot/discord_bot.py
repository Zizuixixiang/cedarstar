"""
Discord 机器人模块。

接收 Discord 消息，调用 LLM 接口生成回复，并发送回 Discord。
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

import discord
from discord.ext import commands

from config import config, validate_config
from llm.llm_interface import LLMInterface
from memory.database import save_message, get_recent_messages, clear_session_messages
from memory.micro_batch import trigger_micro_batch_check


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
        
        # 创建 LLM 接口
        self.llm = LLMInterface()
        
        # 存储对话历史（按用户ID和频道ID）
        self.conversation_histories = {}
        
        # 设置事件处理器
        self._setup_handlers()
        
        logger.info(f"Discord 机器人初始化完成，使用模型: {self.llm.model_name}")
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
            
            # 设置机器人状态
            activity = discord.Game(name="与用户聊天")
            await self.bot.change_presence(activity=activity)
        
        @self.bot.event
        async def on_message(message):
            """
            收到消息时触发。
            
            Args:
                message: Discord 消息对象
            """
            # 忽略机器人自己的消息
            if message.author == self.bot.user:
                return
            
            # 检查是否提及机器人或是在私聊中
            is_mentioned = self.bot.user in message.mentions
            is_dm = isinstance(message.channel, discord.DMChannel)
            
            # 如果消息提及机器人或是在私聊中，则回复
            if is_mentioned or is_dm:
                # 显示"正在输入"状态
                async with message.channel.typing():
                    # 生成回复
                    reply = await self._generate_reply(message)
                    
                    # 发送回复
                    if reply:
                        # 如果回复太长，分割成多个消息
                        if len(reply) > 2000:
                            chunks = self._split_message(reply)
                            for chunk in chunks:
                                await message.channel.send(chunk)
                                await asyncio.sleep(0.5)  # 避免速率限制
                        else:
                            await message.channel.send(reply)
            
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
            model_info = (
                f"🤖 **当前模型**: {self.llm.model_name}\n"
                f"📊 **最大 token**: {self.llm.max_tokens}\n"
                f"🌡️ **温度**: {self.llm.temperature}\n"
                f"⏱️ **超时**: {self.llm.timeout}秒"
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
    
    async def _generate_reply(self, message: discord.Message) -> Optional[str]:
        """
        生成回复消息。
        
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
            
            # 从数据库获取最近的对话历史
            recent_messages = get_recent_messages(session_id, limit=config.MAX_HISTORY_MESSAGES)
            
            # 转换为LLM接口期望的格式
            history = []
            for msg in recent_messages:
                role = "user" if msg['role'] == "user" else "assistant"
                history.append({"role": role, "content": msg['content']})
            
            # 生成回复
            reply, updated_history = self.llm.chat(content, history)
            
            # 保存用户消息到数据库
            save_message(
                session_id=session_id,
                role="user",
                content=content,
                user_id=str(message.author.id),
                channel_id=str(message.channel.id),
                message_id=str(message.id),
                character_id="sirius"
            )
            
            # 保存AI回复到数据库
            save_message(
                session_id=session_id,
                role="assistant",
                content=reply,
                user_id=str(message.author.id),
                channel_id=str(message.channel.id),
                message_id=f"ai_{message.id}",
                character_id="sirius"
            )
            
            logger.info(f"为用户 {message.author.name} 生成回复，历史消息数量: {len(recent_messages) + 2}")
            
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