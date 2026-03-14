"""
LLM 接口模块。

封装 AI API 调用，提供统一的 LLM 接口。
支持多种模型，配置项从 config.py 读取。
"""

import json
import logging
from typing import Dict, List, Optional, Any, Union, Tuple
from dataclasses import dataclass

import requests
from config import config


# 设置日志
logger = logging.getLogger(__name__)


@dataclass
class LLMResponse:
    """LLM 响应数据结构。"""
    
    content: str
    """模型生成的文本内容。"""
    
    model: str
    """使用的模型名称。"""
    
    usage: Optional[Dict[str, int]] = None
    """token 使用情况统计。"""
    
    finish_reason: Optional[str] = None
    """生成结束原因。"""
    
    raw_response: Optional[Dict[str, Any]] = None
    """原始 API 响应数据。"""
    
    def to_dict(self) -> Dict[str, Any]:
        """
        将响应转换为字典。
        
        Returns:
            Dict[str, Any]: 字典格式的响应数据
        """
        return {
            "content": self.content,
            "model": self.model,
            "usage": self.usage,
            "finish_reason": self.finish_reason,
            "raw_response": self.raw_response
        }


class LLMInterface:
    """
    LLM 接口类。
    
    封装 AI API 调用，提供统一的接口。
    """
    
    def __init__(self, model_name: Optional[str] = None):
        """
        初始化 LLM 接口。
        
        Args:
            model_name: 模型名称，如果为 None 则使用 config 中的默认值
        """
        self.model_name = model_name or config.LLM_MODEL_NAME
        self.api_key = config.LLM_API_KEY
        self.api_base = config.LLM_API_BASE
        self.timeout = config.LLM_TIMEOUT
        self.max_tokens = config.LLM_MAX_TOKENS
        self.temperature = config.LLM_TEMPERATURE
        
        # 验证配置
        if not self.api_key:
            logger.warning("LLM_API_KEY 未设置，LLM 功能可能无法正常工作")
        
        # 设置默认 API 基础 URL
        if not self.api_base:
            if "gpt-3.5" in self.model_name or "gpt-4" in self.model_name:
                self.api_base = "https://api.openai.com/v1"
            elif "claude" in self.model_name.lower():
                self.api_base = "https://api.anthropic.com/v1"
            else:
                self.api_base = "https://api.openai.com/v1"
                logger.warning(f"未知模型 {self.model_name}，使用 OpenAI API 作为默认")
    
    def _prepare_headers(self) -> Dict[str, str]:
        """
        准备请求头。
        
        Returns:
            Dict[str, str]: 请求头字典
        """
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "CedarStar/0.1.0"
        }
        
        # 添加认证头
        if "gpt-3.5" in self.model_name or "gpt-4" in self.model_name:
            headers["Authorization"] = f"Bearer {self.api_key}"
        elif "claude" in self.model_name.lower():
            headers["x-api-key"] = self.api_key
            headers["anthropic-version"] = "2023-06-01"
        else:
            headers["Authorization"] = f"Bearer {self.api_key}"
        
        return headers
    
    def _prepare_openai_payload(self, messages: List[Dict[str, str]]) -> Dict[str, Any]:
        """
        准备 OpenAI 兼容 API 的请求负载。
        
        Args:
            messages: 消息列表
            
        Returns:
            Dict[str, Any]: 请求负载
        """
        return {
            "model": self.model_name,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": False
        }
    
    def _prepare_anthropic_payload(self, messages: List[Dict[str, str]]) -> Dict[str, Any]:
        """
        准备 Anthropic Claude API 的请求负载。
        
        Args:
            messages: 消息列表
            
        Returns:
            Dict[str, Any]: 请求负载
        """
        # 转换消息格式
        system_message = None
        claude_messages = []
        
        for msg in messages:
            if msg["role"] == "system":
                system_message = msg["content"]
            else:
                claude_messages.append({
                    "role": msg["role"],
                    "content": msg["content"]
                })
        
        payload = {
            "model": self.model_name,
            "messages": claude_messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature
        }
        
        if system_message:
            payload["system"] = system_message
        
        return payload
    
    def _parse_openai_response(self, response_data: Dict[str, Any]) -> LLMResponse:
        """
        解析 OpenAI 兼容 API 的响应。
        
        Args:
            response_data: API 响应数据
            
        Returns:
            LLMResponse: 解析后的响应
        """
        choice = response_data["choices"][0]
        message = choice["message"]
        
        return LLMResponse(
            content=message["content"],
            model=response_data["model"],
            usage=response_data.get("usage"),
            finish_reason=choice.get("finish_reason"),
            raw_response=response_data
        )
    
    def _parse_anthropic_response(self, response_data: Dict[str, Any]) -> LLMResponse:
        """
        解析 Anthropic Claude API 的响应。
        
        Args:
            response_data: API 响应数据
            
        Returns:
            LLMResponse: 解析后的响应
        """
        content_block = response_data["content"][0]
        
        return LLMResponse(
            content=content_block["text"],
            model=response_data["model"],
            usage={
                "input_tokens": response_data.get("usage", {}).get("input_tokens", 0),
                "output_tokens": response_data.get("usage", {}).get("output_tokens", 0)
            },
            finish_reason=response_data.get("stop_reason"),
            raw_response=response_data
        )
    
    def generate(
        self, 
        prompt: str, 
        system_prompt: Optional[str] = None,
        conversation_history: Optional[List[Dict[str, str]]] = None
    ) -> LLMResponse:
        """
        生成文本响应。
        
        Args:
            prompt: 用户提示
            system_prompt: 系统提示，用于指导模型行为
            conversation_history: 对话历史，格式为 [{"role": "user", "content": "..."}, ...]
            
        Returns:
            LLMResponse: LLM 响应
            
        Raises:
            ValueError: 如果 API 密钥未设置
            requests.exceptions.RequestException: 如果 API 调用失败
        """
        if not self.api_key:
            raise ValueError("LLM_API_KEY 未设置，无法调用 LLM API")
        
        # 构建消息列表
        messages = []
        
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        
        # 添加对话历史
        if conversation_history:
            messages.extend(conversation_history)
        
        # 添加当前提示
        messages.append({"role": "user", "content": prompt})
        
        # 准备请求
        headers = self._prepare_headers()
        
        # 根据模型类型准备不同的负载
        if "claude" in self.model_name.lower():
            endpoint = f"{self.api_base}/messages"
            payload = self._prepare_anthropic_payload(messages)
            parse_func = self._parse_anthropic_response
        else:
            endpoint = f"{self.api_base}/chat/completions"
            payload = self._prepare_openai_payload(messages)
            parse_func = self._parse_openai_response
        
        # 发送请求
        logger.debug(f"调用 LLM API: {endpoint}, 模型: {self.model_name}")
        
        try:
            response = requests.post(
                endpoint,
                headers=headers,
                json=payload,
                timeout=self.timeout
            )
            response.raise_for_status()
            
            response_data = response.json()
            logger.debug(f"LLM API 响应: {response.status_code}")
            
            return parse_func(response_data)
            
        except requests.exceptions.Timeout:
            logger.error(f"LLM API 调用超时: {self.timeout}秒")
            raise
        except requests.exceptions.RequestException as e:
            logger.error(f"LLM API 调用失败: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"响应状态码: {e.response.status_code}")
                logger.error(f"响应内容: {e.response.text}")
            raise
    
    def generate_simple(self, prompt: str) -> str:
        """
        简化版的生成方法，只返回文本内容。
        
        Args:
            prompt: 用户提示
            
        Returns:
            str: 模型生成的文本内容
            
        Raises:
            ValueError: 如果 API 密钥未设置
            requests.exceptions.RequestException: 如果 API 调用失败
        """
        response = self.generate(prompt)
        return response.content
    
    def chat(
        self, 
        message: str, 
        history: Optional[List[Dict[str, str]]] = None
    ) -> Tuple[str, List[Dict[str, str]]]:
        """
        聊天接口，维护对话历史。
        
        Args:
            message: 用户消息
            history: 对话历史，如果为 None 则创建新的历史
            
        Returns:
            Tuple[str, List[Dict[str, str]]]: (模型回复, 更新后的对话历史)
            
        Raises:
            ValueError: 如果 API 密钥未设置
            requests.exceptions.RequestException: 如果 API 调用失败
        """
        if history is None:
            history = []
        
        # 生成回复（generate函数会自动添加用户消息到消息列表）
        response = self.generate(message, conversation_history=history)
        
        # 将用户消息和助手回复都添加到历史记录中
        history.append({"role": "user", "content": message})
        history.append({"role": "assistant", "content": response.content})
        
        return response.content, history
    
    def generate_with_context(self, messages: List[Dict[str, str]]) -> str:
        """
        使用完整的 messages 数组生成回复。
        
        这个方法专门用于 context builder 构建的完整消息数组。
        
        Args:
            messages: 完整的消息数组，包含 system、user、assistant 消息
            
        Returns:
            str: 模型生成的文本内容
            
        Raises:
            ValueError: 如果 API 密钥未设置
            requests.exceptions.RequestException: 如果 API 调用失败
        """
        if not self.api_key:
            raise ValueError("LLM_API_KEY 未设置，无法调用 LLM API")
        
        # 准备请求
        headers = self._prepare_headers()
        
        # 根据模型类型准备不同的负载
        if "claude" in self.model_name.lower():
            endpoint = f"{self.api_base}/messages"
            payload = self._prepare_anthropic_payload(messages)
            parse_func = self._parse_anthropic_response
        else:
            endpoint = f"{self.api_base}/chat/completions"
            payload = self._prepare_openai_payload(messages)
            parse_func = self._parse_openai_response
        
        # 发送请求
        logger.debug(f"调用 LLM API (with context): {endpoint}, 模型: {self.model_name}")
        logger.debug(f"消息数量: {len(messages)}")
        
        try:
            response = requests.post(
                endpoint,
                headers=headers,
                json=payload,
                timeout=self.timeout
            )
            response.raise_for_status()
            
            response_data = response.json()
            logger.debug(f"LLM API 响应: {response.status_code}")
            
            llm_response = parse_func(response_data)
            return llm_response.content
            
        except requests.exceptions.Timeout:
            logger.error(f"LLM API 调用超时: {self.timeout}秒")
            raise
        except requests.exceptions.RequestException as e:
            logger.error(f"LLM API 调用失败: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"响应状态码: {e.response.status_code}")
                logger.error(f"响应内容: {e.response.text}")
            raise


# 创建全局 LLM 接口实例
llm = LLMInterface()


def test_llm_interface() -> None:
    """
    测试 LLM 接口功能。
    
    这是一个简单的测试函数，用于验证 LLM 接口是否正常工作。
    """
    print("测试 LLM 接口...")
    
    try:
        # 检查配置
        if not config.LLM_API_KEY:
            print("警告: LLM_API_KEY 未设置，跳过实际 API 调用测试")
            print("测试通过（配置检查）")
            return
        
        # 创建测试实例
        test_llm = LLMInterface()
        
        # 测试简单生成
        print(f"使用模型: {test_llm.model_name}")
        print("测试简单提示...")
        
        response = test_llm.generate_simple("你好，请用中文回复。")
        print(f"回复: {response[:50]}...")
        
        print("LLM 接口测试通过")
        
    except Exception as e:
        print(f"LLM 接口测试失败: {e}")


if __name__ == "__main__":
    """LLM 接口模块测试入口。"""
    test_llm_interface()