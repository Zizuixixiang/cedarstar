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
            return int(os.getenv("LLM_TIMEOUT", "30"))
        except ValueError:
            return 30
    
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
    except ValueError as e:
        print(f"配置验证失败: {e}")