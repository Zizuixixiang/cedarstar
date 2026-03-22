"""
配置文件模块。

使用 python-dotenv 读取 .env 文件中的配置项。
所有配置项统一在此管理，避免硬编码。
"""

import os
from typing import Optional
from dotenv import load_dotenv

# 加载 .env 文件
load_dotenv()


class Config:
    """配置类，封装所有环境变量读取逻辑。"""
    
    # Discord 配置
    @property
    def DISCORD_BOT_TOKEN(self) -> str:
        """
        获取 Discord 机器人令牌。
        
        Returns:
            str: Discord 机器人令牌
            
        Raises:
            ValueError: 如果 DISCORD_BOT_TOKEN 未设置
        """
        token = os.getenv("DISCORD_BOT_TOKEN")
        if not token:
            raise ValueError("DISCORD_BOT_TOKEN 未在 .env 文件中设置")
        return token
    
    # ChromaDB 配置
    @property
    def CHROMADB_URL(self) -> Optional[str]:
        """
        获取 ChromaDB 连接 URL。
        
        Returns:
            Optional[str]: ChromaDB 连接 URL，如果未设置则返回 None
        """
        return os.getenv("CHROMADB_URL")
    
    # 数据库配置
    @property
    def DATABASE_URL(self) -> Optional[str]:
        """
        获取数据库连接 URL。
        
        Returns:
            Optional[str]: 数据库连接 URL，如果未设置则返回 None
        """
        return os.getenv("DATABASE_URL")
    
    # LLM 配置
    @property
    def LLM_MODEL_NAME(self) -> str:
        """
        获取 LLM 模型名称。
        
        Returns:
            str: LLM 模型名称，默认为 "gpt-3.5-turbo"
        """
        return os.getenv("LLM_MODEL_NAME", "gpt-3.5-turbo")
    
    @property
    def LLM_API_KEY(self) -> Optional[str]:
        """
        获取 LLM API 密钥。
        
        Returns:
            Optional[str]: LLM API 密钥，如果未设置则返回 None
        """
        return os.getenv("LLM_API_KEY")
    
    @property
    def LLM_API_BASE(self) -> Optional[str]:
        """
        获取 LLM API 基础 URL。
        
        Returns:
            Optional[str]: LLM API 基础 URL，如果未设置则返回 None
        """
        return os.getenv("LLM_API_BASE")
    
    @property
    def LLM_TIMEOUT(self) -> int:
        """
        获取 LLM API 调用超时时间（秒）。
        
        Returns:
            int: 超时时间，默认为 30 秒
        """
        try:
            return int(os.getenv("LLM_TIMEOUT", "60"))
        except ValueError:
            return 60

    @property
    def LLM_VISION_TIMEOUT(self) -> int:
        """
        含图片等多模态请求时的读超时（秒），与 LLM_TIMEOUT 取较大值生效。
        贴纸识图、相册多模态等经公网 VL 常明显慢于纯文本，默认 180；可通过环境变量 LLM_VISION_TIMEOUT 调整。
        """
        try:
            return int(os.getenv("LLM_VISION_TIMEOUT", "180"))
        except ValueError:
            return 180

    @property
    def OPENAI_API_KEY(self) -> Optional[str]:
        """OpenAI API 密钥；用于语音转录（STT）在库内无 stt 配置时的回退，不复用 LLM_API_KEY。"""
        return os.getenv("OPENAI_API_KEY")

    @property
    def OPENAI_API_BASE(self) -> str:
        """OpenAI 兼容 API 根路径（含 /v1）；STT 回退用，默认 https://api.openai.com/v1。"""
        return os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1")
    
    @property
    def LLM_MAX_TOKENS(self) -> int:
        """
        获取 LLM 最大生成 token 数。
        
        Returns:
            int: 最大 token 数，默认为 1000
        """
        try:
            return int(os.getenv("LLM_MAX_TOKENS", "1000"))
        except ValueError:
            return 1000
    
    @property
    def LLM_TEMPERATURE(self) -> float:
        """
        获取 LLM 温度参数。
        
        Returns:
            float: 温度参数，默认为 0.7
        """
        try:
            return float(os.getenv("LLM_TEMPERATURE", "0.7"))
        except ValueError:
            return 0.7
    
    # 应用配置
    @property
    def DEBUG(self) -> bool:
        """
        获取调试模式设置。
        
        Returns:
            bool: 是否启用调试模式，默认为 False
        """
        return os.getenv("DEBUG", "False").lower() in ("true", "1", "yes")
    
    @property
    def LOG_LEVEL(self) -> str:
        """
        获取日志级别。
        
        Returns:
            str: 日志级别，默认为 "INFO"
        """
        return os.getenv("LOG_LEVEL", "INFO").upper()
    
    @property
    def MAX_HISTORY_MESSAGES(self) -> int:
        """
        获取最大历史消息数量。
        
        Returns:
            int: 最大历史消息数量，默认为 20
        """
        try:
            return int(os.getenv("MAX_HISTORY_MESSAGES", "20"))
        except ValueError:
            return 20
    
    # 摘要 API 配置（用于微批处理）
    @property
    def SUMMARY_API_KEY(self) -> Optional[str]:
        """
        获取摘要 API 密钥。
        
        Returns:
            Optional[str]: 摘要 API 密钥，如果未设置则返回 None
        """
        return os.getenv("SUMMARY_API_KEY")
    
    @property
    def SUMMARY_API_BASE(self) -> Optional[str]:
        """
        获取摘要 API 基础 URL。
        
        Returns:
            Optional[str]: 摘要 API 基础 URL，如果未设置则返回 None
        """
        return os.getenv("SUMMARY_API_BASE")
    
    @property
    def SUMMARY_MODEL_NAME(self) -> str:
        """
        获取摘要模型名称。
        
        Returns:
            str: 摘要模型名称，默认为 "gpt-3.5-turbo"
        """
        return os.getenv("SUMMARY_MODEL_NAME", "gpt-3.5-turbo")
    
    @property
    def SUMMARY_TIMEOUT(self) -> int:
        """
        获取摘要 API 调用超时时间（秒）。
        
        Returns:
            int: 超时时间，默认为 60 秒
        """
        try:
            return int(os.getenv("SUMMARY_TIMEOUT", "60"))
        except ValueError:
            return 60
    
    @property
    def SUMMARY_MAX_TOKENS(self) -> int:
        """
        获取摘要最大生成 token 数。
        
        Returns:
            int: 最大 token 数，默认为 500
        """
        try:
            return int(os.getenv("SUMMARY_MAX_TOKENS", "500"))
        except ValueError:
            return 500
    
    # 微批处理配置
    @property
    def MICRO_BATCH_THRESHOLD(self) -> int:
        """
        获取微批处理触发阈值。
        
        当某个 session 中 is_summarized=0 的消息达到此数量时触发微批处理。
        
        Returns:
            int: 微批处理阈值，默认为 50
        """
        try:
            return int(os.getenv("MICRO_BATCH_THRESHOLD", "50"))
        except ValueError:
            return 50
    
    @property
    def MESSAGE_BUFFER_DELAY(self) -> int:
        """
        获取消息缓冲延迟时间（秒）。
        
        收到消息后等待此时间，期间如果同一 session 有新消息进来就重置计时器，
        超时后才将缓冲区内所有消息合并成一条处理。
        
        Returns:
            int: 缓冲延迟时间，默认为 5 秒
        """
        try:
            return int(os.getenv("MESSAGE_BUFFER_DELAY", "5"))
        except ValueError:
            return 5
    
    # System Prompt 配置
    @property
    def SYSTEM_PROMPT(self) -> str:
        """
        获取系统提示词。
        
        Returns:
            str: 系统提示词，默认为通用助手提示词
        """
        return os.getenv("SYSTEM_PROMPT", "你是一个友善且有帮助的AI助手。")
    
    # Context 构建配置
    @property
    def CONTEXT_MAX_RECENT_MESSAGES(self) -> int:
        """
        获取 context 构建时最多包含的最近消息数。
        
        Returns:
            int: 最大最近消息数，默认为 40
        """
        try:
            return int(os.getenv("CONTEXT_MAX_RECENT_MESSAGES", "40"))
        except ValueError:
            return 40
    
    @property
    def CONTEXT_MAX_DAILY_SUMMARIES(self) -> int:
        """
        获取 context 构建时最多包含的每日摘要数。
        
        Returns:
            int: 最大每日摘要数，默认为 5
        """
        try:
            return int(os.getenv("CONTEXT_MAX_DAILY_SUMMARIES", "5"))
        except ValueError:
            return 5
    
    # 智谱 AI 配置（用于 Embedding）
    @property
    def ZHIPU_API_KEY(self) -> Optional[str]:
        """
        获取智谱 AI API 密钥。
        
        用于调用智谱 embedding-3 模型生成向量。
        
        Returns:
            Optional[str]: 智谱 API 密钥，如果未设置则返回 None
        """
        return os.getenv("ZHIPU_API_KEY")
    
    # Cohere Rerank API 配置
    @property
    def COHERE_API_KEY(self) -> Optional[str]:
        """
        获取 Cohere Rerank API 密钥。
        
        用于调用 Cohere Rerank API 进行文档重排序。
        
        Returns:
            Optional[str]: Cohere API 密钥，如果未设置则返回 None
        """
        return os.getenv("COHERE_API_KEY")
    
    # Telegram 配置
    @property
    def TELEGRAM_BOT_TOKEN(self) -> Optional[str]:
        """
        获取 Telegram 机器人令牌。
        
        获取方式：https://t.me/BotFather
        1. 与 BotFather 对话
        2. 发送 /newbot 创建新机器人
        3. 按提示设置名称和用户名
        4. 获取 Token
        
        Returns:
            Optional[str]: Telegram 机器人令牌，如果未设置则返回 None
        """
        return os.getenv("TELEGRAM_BOT_TOKEN")
    
    # ChromaDB 本地存储配置
    @property
    def CHROMADB_PERSIST_DIR(self) -> str:
        """
        获取 ChromaDB 本地持久化目录。
        
        Returns:
            str: ChromaDB 数据目录路径，默认为 cedarstar/chroma_db/
        """
        return os.getenv("CHROMADB_PERSIST_DIR", "chroma_db")
    
    # 代理配置
    @property
    def HTTP_PROXY(self) -> Optional[str]:
        """
        获取 HTTP 代理服务器地址。
        
        Returns:
            Optional[str]: HTTP 代理地址，如果未设置则返回 None
        """
        return os.getenv("HTTP_PROXY")
    
    @property
    def HTTPS_PROXY(self) -> Optional[str]:
        """
        获取 HTTPS 代理服务器地址。
        
        Returns:
            Optional[str]: HTTPS 代理地址，如果未设置则返回 None
        """
        return os.getenv("HTTPS_PROXY")
    
    @property
    def ENABLE_PROXY(self) -> bool:
        """
        获取是否启用代理设置。
        
        Returns:
            bool: 是否启用代理，默认为 True
        """
        return os.getenv("ENABLE_PROXY", "True").lower() in ("true", "1", "yes")
    
    @property
    def proxy_dict(self) -> Optional[dict]:
        """
        获取代理配置字典。
        
        Returns:
            Optional[dict]: 代理配置字典，格式为 {'http': 'http://localhost:7897', 'https': 'http://localhost:7897'}
            如果未启用代理则返回 None
        """
        if not self.ENABLE_PROXY:
            return None
        
        http_proxy = self.HTTP_PROXY
        https_proxy = self.HTTPS_PROXY
        
        if not http_proxy and not https_proxy:
            return None
        
        proxies = {}
        if http_proxy:
            proxies['http'] = http_proxy
        if https_proxy:
            proxies['https'] = https_proxy
        
        return proxies

    @property
    def DEFAULT_CHARACTER_ID(self) -> str:
        """
        无激活 chat 配置或 persona_id 为空时，写入 messages.character_id 的兜底值。
        环境变量 DEFAULT_CHARACTER_ID；未设置或非空字符串无效时默认为 sirius。
        """
        v = (os.getenv("DEFAULT_CHARACTER_ID") or "").strip()
        return v if v else "sirius"


# 平台常量定义
class Platform:
    """
    平台常量定义。
    
    所有写入 platform 字段的地方必须引用这个常量，不允许直接写字符串字面量。
    """
    TELEGRAM = "telegram"
    DISCORD = "discord"
    BATCH = "batch"
    SYSTEM = "system"
    RIKKAHUB = "rikkahub"


# 创建全局配置实例
config = Config()


def validate_config() -> None:
    """
    验证配置是否完整。
    
    检查必要的配置项是否已设置。
    
    Raises:
        ValueError: 如果必要配置项缺失
    """
    # 验证 Discord 配置
    try:
        config.DISCORD_BOT_TOKEN
    except ValueError as e:
        raise ValueError(f"Discord 配置验证失败: {e}")
    
    # 验证 LLM 配置
    if not config.LLM_API_KEY:
        print("警告: LLM_API_KEY 未设置，LLM 功能可能无法正常工作")
    
    # 验证数据库配置
    if not config.DATABASE_URL:
        print("警告: DATABASE_URL 未设置，数据库功能可能无法正常工作")
    
    if not config.CHROMADB_URL:
        print("警告: CHROMADB_URL 未设置，向量数据库功能可能无法正常工作")
    
    # 验证代理配置
    if config.ENABLE_PROXY:
        if config.HTTP_PROXY or config.HTTPS_PROXY:
            print(f"代理已启用: HTTP={config.HTTP_PROXY}, HTTPS={config.HTTPS_PROXY}")
        else:
            print("警告: 代理已启用但未配置代理地址")


if __name__ == "__main__":
    """配置模块测试入口。"""
    try:
        validate_config()
        print("配置验证通过")
        print(f"Discord Token: {'已设置' if config.DISCORD_BOT_TOKEN else '未设置'}")
        print(f"LLM 模型: {config.LLM_MODEL_NAME}")
        print(f"调试模式: {config.DEBUG}")
        print(f"代理配置: {config.proxy_dict}")
        print(f"System Prompt: {config.SYSTEM_PROMPT[:50]}...")
    except ValueError as e:
        print(f"配置验证失败: {e}")
