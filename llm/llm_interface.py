"""
LLM 接口模块。

封装 AI API 调用，提供统一的 LLM 接口。
支持多种模型，配置项从 config.py 读取。
"""

import json
import logging
import asyncio
from typing import Any, Dict, Generator, List, Optional, Tuple, Union
from dataclasses import dataclass

import requests
from bot.logutil import exc_detail
from config import config, Platform
from memory.database import get_database


# 设置日志
logger = logging.getLogger(__name__)


def use_anthropic_messages_api(
    api_base: Optional[str], model_name: Optional[str]
) -> bool:
    """
    是否走 Anthropic Messages API（/messages）。
    优先看 api_base 是否含 anthropic；否则根据模型名含 claude 回退（兼容旧配置）。
    """
    b = (api_base or "").lower()
    if "anthropic" in b:
        return True
    if "claude" in (model_name or "").lower():
        return True
    return False


def build_user_multimodal_content(
    api_base: Optional[str],
    model_name: Optional[str],
    text: str,
    image_payloads: List[Dict[str, Any]],
) -> Union[str, List[Dict[str, Any]]]:
    """
    组装用户多模态 content（OpenAI 兼容：text + image_url；Claude：text + image base64）。

    image_payload 项：`data`（base64 字符串）、可选 `mime_type`（默认 image/jpeg）、可选 `caption`（已并入 text 时不必重复）。
    """
    if not image_payloads:
        return text
    t = (text or "").strip()
    anthropic_fmt = use_anthropic_messages_api(api_base, model_name)
    parts: List[Dict[str, Any]] = []
    if t:
        parts.append({"type": "text", "text": t})
    for img in image_payloads:
        mime = img.get("mime_type") or "image/jpeg"
        b64 = img.get("data") or ""
        if not b64:
            continue
        if anthropic_fmt:
            parts.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": mime,
                        "data": b64,
                    },
                }
            )
        else:
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"},
                }
            )
    if not parts:
        return t or "请描述图片。"
    if not any(p.get("type") == "text" for p in parts):
        parts.insert(0, {"type": "text", "text": "请查看图片并作答。"})
    return parts


def messages_contain_multimodal_images(messages: List[Dict[str, Any]]) -> bool:
    """判断 messages 中是否含 OpenAI/Claude 风格的多模态图片块。"""
    for msg in messages:
        c = msg.get("content")
        if not isinstance(c, list):
            continue
        for part in c:
            if not isinstance(part, dict):
                continue
            t = part.get("type")
            if t in ("image_url", "image"):
                return True
    return False


def _openai_tools_specs_to_anthropic(
    tools: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """OpenAI `tools`（type+function）→ Anthropic Messages API `tools`（name+input_schema）。"""
    out: List[Dict[str, Any]] = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else {}
        name = (fn.get("name") or "").strip()
        if not name:
            continue
        desc = (fn.get("description") or "").strip()
        params = fn.get("parameters")
        if not isinstance(params, dict):
            params = {"type": "object", "properties": {}}
        out.append(
            {
                "name": name,
                "description": desc,
                "input_schema": params,
            }
        )
    return out


def _openai_chat_messages_to_anthropic_messages(
    messages: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    OpenAI chat 多轮（含 role=tool、assistant.tool_calls）→ Anthropic `messages` 数组。
    不含 system；system 由调用方单独放入 payload。
    """
    out: List[Dict[str, Any]] = []
    i = 0
    n = len(messages)
    while i < n:
        m = messages[i]
        role = m.get("role")
        if role == "tool":
            blocks: List[Dict[str, Any]] = []
            while i < n and messages[i].get("role") == "tool":
                tmsg = messages[i]
                raw = tmsg.get("content")
                cstr = raw if isinstance(raw, str) else json.dumps(
                    raw, ensure_ascii=False
                )
                blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": (tmsg.get("tool_call_id") or "") or "",
                        "content": cstr,
                    }
                )
                i += 1
            out.append({"role": "user", "content": blocks})
            continue
        if role == "assistant":
            tcalls = m.get("tool_calls")
            has_tc = isinstance(tcalls, list) and len(tcalls) > 0
            c = m.get("content")
            if not has_tc:
                if c is None:
                    c = ""
                out.append({"role": "assistant", "content": c})
                i += 1
                continue
            parts: List[Dict[str, Any]] = []
            if isinstance(c, str) and c.strip():
                parts.append({"type": "text", "text": c})
            for tc in tcalls:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                name = (fn.get("name") or tc.get("name") or "").strip()
                tid = tc.get("id") or ""
                raw_args = fn.get("arguments", "{}")
                if not isinstance(raw_args, str):
                    raw_args = json.dumps(raw_args, ensure_ascii=False)
                try:
                    inp = json.loads(raw_args) if raw_args.strip() else {}
                except json.JSONDecodeError:
                    inp = {}
                if not name:
                    continue
                parts.append(
                    {"type": "tool_use", "id": tid, "name": name, "input": inp}
                )
            if not parts:
                parts.append({"type": "text", "text": ""})
            out.append({"role": "assistant", "content": parts})
            i += 1
            continue
        if role == "user":
            out.append({"role": "user", "content": m.get("content")})
            i += 1
            continue
        out.append(
            {
                "role": role if role else "user",
                "content": m.get("content") if m.get("content") is not None else "",
            }
        )
        i += 1
    return out


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

    tool_calls: Optional[List[Dict[str, Any]]] = None
    """OpenAI 风格 tool_calls，每项含 id、name、arguments(str)；无则为 None。"""

    thinking: Optional[str] = None
    """思维链（如 R1 reasoning）；由 generate_with_context_and_tracking 等填充。"""

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
            "raw_response": self.raw_response,
            "tool_calls": self.tool_calls,
            "thinking": self.thinking,
        }


class LLMInterface:
    """
    LLM 接口类。
    
    封装 AI API 调用，提供统一的接口。
    
    Attributes:
        character_id: 与当前激活 api_configs 行中 persona_id 对应的字符串，
            供 Bot 写入 messages.character_id；无有效 persona_id 时为 "sirius"。
    """
    
    def __init__(self, model_name: Optional[str] = None, config_type: str = 'chat'):
        """
        初始化 LLM 接口。
        
        优先从数据库激活的 api_config 读取配置；若数据库无激活配置，
        则回退到 .env / config.py 中的环境变量。
        
        Args:
            model_name: 模型名称，覆盖自动检测（可选）
            config_type: 配置类型，`chat` / `summary` / `vision`（视觉/多模态等，库内独立激活行）
        """
        # 尝试从数据库激活配置读取（含 persona_id，供 messages.character_id 使用）
        db_cfg = self._load_active_config(config_type)
        
        if db_cfg:
            self.model_name = model_name or db_cfg.get('model') or config.LLM_MODEL_NAME
            self.api_key = db_cfg.get('api_key') or config.LLM_API_KEY
            self.api_base = db_cfg.get('base_url') or config.LLM_API_BASE
            logger.info(f"LLMInterface 使用数据库激活配置: [{db_cfg.get('name')}] "
                        f"config_type={config_type}, model={self.model_name}, base_url={self.api_base}")
        else:
            # 回退到环境变量
            if config_type == 'summary':
                self.model_name = model_name or config.SUMMARY_MODEL_NAME
                self.api_key = config.SUMMARY_API_KEY or config.LLM_API_KEY
                self.api_base = config.SUMMARY_API_BASE or config.LLM_API_BASE
            else:
                # chat、vision：无库内激活行时共用主对话环境变量（vision 建议在 Settings 单独配置）
                self.model_name = model_name or config.LLM_MODEL_NAME
                self.api_key = config.LLM_API_KEY
                self.api_base = config.LLM_API_BASE
            logger.info(f"LLMInterface 使用环境变量配置: config_type={config_type}, model={self.model_name}")
        
        # 与当前激活 api_configs 关联的人设 ID，写入消息表时作为 character_id（无则兜底 sirius）
        self.character_id = self._resolve_character_id_from_config(db_cfg)
        
        self.timeout = config.LLM_TIMEOUT
        # vision 专用配置：读超时至少与 LLM_VISION_TIMEOUT 对齐（贴纸识图等为同步阻塞）
        if config_type == "vision":
            self.timeout = max(self.timeout, config.LLM_VISION_TIMEOUT)
        self.max_tokens = config.LLM_MAX_TOKENS
        self.temperature = config.LLM_TEMPERATURE
        
        # 验证配置
        if not self.api_key:
            logger.warning("API Key 未设置，LLM 功能可能无法正常工作")
        
        # 设置默认 API 基础 URL
        if not self.api_base:
            if "gpt-3.5" in self.model_name or "gpt-4" in self.model_name:
                self.api_base = "https://api.openai.com/v1"
            elif "claude" in self.model_name.lower():
                self.api_base = "https://api.anthropic.com/v1"
            else:
                self.api_base = "https://api.openai.com/v1"
                logger.warning(f"未知模型 {self.model_name}，使用 OpenAI API 作为默认")

    @staticmethod
    def _load_active_config(config_type: str = 'chat') -> Optional[Dict[str, Any]]:
        """
        从数据库读取指定类型的激活 api_config 完整行。

        返回字典包含 `api_configs` 表各字段（含 `persona_id`），供模型参数与
        `character_id` 解析使用；失败或无激活行时返回 None。
        """
        try:
            from memory.database import get_database
            db = get_database()
            return db.get_active_api_config(config_type)
        except Exception as e:
            logger.warning(f"从数据库读取激活 API 配置失败，将使用环境变量: {e}")
            return None

    @staticmethod
    def _resolve_character_id_from_config(db_cfg: Optional[Dict[str, Any]]) -> str:
        """
        从激活配置中的 persona_id 得到写入 messages 的 character_id 字符串。

        persona_id 为空或缺失时返回字符串 "sirius"。
        """
        if not db_cfg:
            return "sirius"
        pid = db_cfg.get("persona_id")
        if pid is None:
            return "sirius"
        s = str(pid).strip()
        if not s or s.lower() == "none":
            return "sirius"
        return s

    def _use_anthropic_messages_api(self) -> bool:
        return use_anthropic_messages_api(self.api_base, self.model_name)

    def _request_timeout_seconds(self, messages: List[Dict[str, Any]]) -> int:
        """含图片时与 LLM_VISION_TIMEOUT 取 max，避免多模态请求被默认短超时切断。"""
        if messages_contain_multimodal_images(messages):
            return max(self.timeout, config.LLM_VISION_TIMEOUT)
        return self.timeout

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
    
    def _prepare_openai_payload(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        准备 OpenAI 兼容 API 的请求负载。
        
        Args:
            messages: 消息列表
            tools: OpenAI tools / function 定义列表；有值时附带 tool_choice=auto
            
        Returns:
            Dict[str, Any]: 请求负载
        """
        payload: Dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            # OpenAI Chat Completions：与 tools 同发，值为字面量 "auto"（由模型决定是否调用工具）
            # 未传 tools 时不应带 tool_choice，故放在本分支内
            payload["tool_choice"] = "auto"
        return payload
    
    def _prepare_anthropic_payload(self, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
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
                c = msg["content"]
                system_message = c if isinstance(c, str) else str(c)
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
        raw_content = message.get("content")
        if raw_content is None:
            text_content = ""
        elif isinstance(raw_content, str):
            text_content = raw_content
        else:
            text_content = str(raw_content)

        tool_calls_out: Optional[List[Dict[str, Any]]] = None
        raw_tc = message.get("tool_calls")
        if raw_tc:
            tool_calls_out = []
            for tc in raw_tc:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                args = fn.get("arguments")
                tool_calls_out.append(
                    {
                        "id": tc.get("id") or "",
                        "name": fn.get("name") or "",
                        "arguments": args if isinstance(args, str) else (json.dumps(args) if args is not None else "{}"),
                    }
                )

        return LLMResponse(
            content=text_content,
            model=response_data["model"],
            usage=response_data.get("usage"),
            finish_reason=choice.get("finish_reason"),
            raw_response=response_data,
            tool_calls=tool_calls_out if tool_calls_out else None,
        )
    
    def _parse_anthropic_response(self, response_data: Dict[str, Any]) -> LLMResponse:
        """
        解析 Anthropic Claude API 的响应。
        
        Args:
            response_data: API 响应数据
            
        Returns:
            LLMResponse: 解析后的响应
        """
        blocks = response_data.get("content") or []
        text_parts: List[str] = []
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(block.get("text") or "")
        merged = "".join(text_parts).strip()
        if not merged and blocks:
            merged = blocks[0].get("text", "") if isinstance(blocks[0], dict) else ""
        
        return LLMResponse(
            content=merged,
            model=response_data["model"],
            usage={
                "input_tokens": response_data.get("usage", {}).get("input_tokens", 0),
                "output_tokens": response_data.get("usage", {}).get("output_tokens", 0)
            },
            finish_reason=response_data.get("stop_reason"),
            raw_response=response_data,
            tool_calls=None,
        )
    
    def generate(
        self, 
        prompt: str, 
        system_prompt: Optional[str] = None,
        conversation_history: Optional[List[Dict[str, str]]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> LLMResponse:
        """
        生成文本响应。
        
        Args:
            prompt: 用户提示
            system_prompt: 系统提示，用于指导模型行为
            conversation_history: 对话历史，格式为 [{"role": "user", "content": "..."}, ...]
            tools: OpenAI 兼容 tools（仅 OpenAI 路径写入 payload；Anthropic 路径忽略）
            
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
        
        # 根据 API 基址 / 模型选择端点
        if self._use_anthropic_messages_api():
            endpoint = f"{self.api_base}/messages"
            payload = self._prepare_anthropic_payload(messages)
            parse_func = self._parse_anthropic_response
        else:
            endpoint = f"{self.api_base}/chat/completions"
            payload = self._prepare_openai_payload(messages, tools)
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
            logger.error(
                "LLM API 调用失败 endpoint=%s model=%s: %s",
                endpoint,
                self.model_name,
                exc_detail(e),
            )
            if hasattr(e, 'response') and e.response is not None:
                logger.error(
                    "HTTP 响应 status=%s body_prefix=%r",
                    e.response.status_code,
                    (e.response.text or "")[:1200],
                )
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
    
    def generate_with_context(self, messages: List[Dict[str, Any]]) -> str:
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
        
        # 根据 API 基址 / 模型选择端点
        if self._use_anthropic_messages_api():
            endpoint = f"{self.api_base}/messages"
            payload = self._prepare_anthropic_payload(messages)
            parse_func = self._parse_anthropic_response
        else:
            endpoint = f"{self.api_base}/chat/completions"
            payload = self._prepare_openai_payload(messages)
            parse_func = self._parse_openai_response
        
        # 发送请求
        req_timeout = self._request_timeout_seconds(messages)
        logger.debug(
            f"调用 LLM API (with context): {endpoint}, 模型: {self.model_name}, timeout={req_timeout}s"
        )
        logger.debug(f"消息数量: {len(messages)}")
        
        try:
            response = requests.post(
                endpoint,
                headers=headers,
                json=payload,
                timeout=req_timeout
            )
            response.raise_for_status()
            
            response_data = response.json()
            logger.debug(f"LLM API 响应: {response.status_code}")
            
            llm_response = parse_func(response_data)
            return llm_response.content
            
        except requests.exceptions.Timeout:
            mm = messages_contain_multimodal_images(messages)
            logger.error(
                "LLM API 调用超时: %s秒%s",
                req_timeout,
                "（请求中含多模态图片）" if mm else "（无多模态图片，多为上下文过大或上游慢）",
            )
            raise
        except requests.exceptions.RequestException as e:
            logger.error(
                "LLM API(with context) 失败 endpoint=%s model=%s: %s",
                endpoint,
                self.model_name,
                exc_detail(e),
            )
            if hasattr(e, 'response') and e.response is not None:
                logger.error(
                    "HTTP 响应 status=%s body_prefix=%r",
                    e.response.status_code,
                    (e.response.text or "")[:1200],
                )
            raise
    
    def _save_token_usage_async(self, usage: Dict[str, int], platform: Optional[str] = None):
        """
        异步保存token使用量到数据库。
        
        若在异步事件循环中则 create_task；若在线程池等无 loop 环境（如 vision 任务里
        run_in_executor 调 LLM）则同步写库，避免 no running event loop。
        
        Args:
            usage: token使用统计字典
            platform: 平台标识（可选）
        """
        try:
            prompt_tokens = usage.get("prompt_tokens", usage.get("input_tokens", 0))
            completion_tokens = usage.get("completion_tokens", usage.get("output_tokens", 0))
            total_tokens = usage.get("total_tokens", prompt_tokens + completion_tokens)

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop is not None and loop.is_running():
                loop.create_task(
                    self._async_save_token_usage(
                        prompt_tokens, completion_tokens, total_tokens, platform
                    )
                )
            else:
                db = get_database()
                db.save_token_usage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    model=self.model_name,
                    platform=platform,
                )
        except Exception as e:
            logger.error("保存 token 使用量失败: %s", exc_detail(e))
    
    async def _async_save_token_usage(self, prompt_tokens: int, completion_tokens: int, 
                                     total_tokens: int, platform: Optional[str] = None):
        """
        异步保存token使用量的实际实现。
        
        Args:
            prompt_tokens: 提示token数
            completion_tokens: 完成token数
            total_tokens: 总token数
            platform: 平台标识（可选）
        """
        try:
            db = get_database()
            db.save_token_usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                model=self.model_name,
                platform=platform
            )
        except Exception as e:
            logger.error("异步保存 token 使用量失败: %s", exc_detail(e))
    
    def _extract_thinking_content(self, response_data: Dict[str, Any]) -> Optional[str]:
        """
        从API响应中提取思维链内容。
        
        支持 DeepSeek R1 的 reasoning_content 字段和 Gemini 的 thinking 字段。
        
        Args:
            response_data: API响应数据
            
        Returns:
            Optional[str]: 思维链内容，如果不支持则返回 None
        """
        try:
            # 检查是否是 DeepSeek R1 模型
            if "reasoning_content" in response_data:
                return response_data.get("reasoning_content")
            
            # 检查是否是 Gemini 模型
            if "thinking" in response_data:
                return response_data.get("thinking")
            
            # 检查 OpenAI 格式的响应中是否有思维链
            if "choices" in response_data and len(response_data["choices"]) > 0:
                choice = response_data["choices"][0]
                if "reasoning_content" in choice:
                    return choice.get("reasoning_content")
                if "thinking" in choice:
                    return choice.get("thinking")
            
            # 不支持思维链
            return None
            
        except Exception as e:
            logger.error("提取思维链内容失败: %s", exc_detail(e))
            return None
    
    def generate_with_token_tracking(
        self, 
        prompt: str, 
        system_prompt: Optional[str] = None,
        conversation_history: Optional[List[Dict[str, str]]] = None,
        platform: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> LLMResponse:
        """
        生成文本响应并跟踪token使用量。
        
        Args:
            prompt: 用户提示
            system_prompt: 系统提示，用于指导模型行为
            conversation_history: 对话历史，格式为 [{"role": "user", "content": "..."}, ...]
            platform: 平台标识（可选）
            tools: OpenAI 兼容 tools（仅 OpenAI 路径生效）
            
        Returns:
            LLMResponse: LLM 响应
            
        Raises:
            ValueError: 如果 API 密钥未设置
            requests.exceptions.RequestException: 如果 API 调用失败
        """
        response = self.generate(
            prompt, system_prompt, conversation_history, tools=tools
        )
        
        # 保存token使用量
        if response.usage:
            self._save_token_usage_async(response.usage, platform)
        
        return response
    
    def generate_with_context_and_tracking(
        self, 
        messages: List[Dict[str, Any]], 
        platform: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> LLMResponse:
        """
        使用完整的 messages 数组生成回复，并跟踪token使用量。
        
        Args:
            messages: 完整的消息数组，包含 system、user、assistant 消息
            platform: 平台标识（可选）
            tools: OpenAI 兼容 tools 列表（仅 OpenAI 路径生效；Anthropic 路径忽略）
            
        Returns:
            LLMResponse: 含 content、可选 tool_calls、thinking 等
            
        Raises:
            ValueError: 如果 API 密钥未设置
            requests.exceptions.RequestException: 如果 API 调用失败
        """
        if not self.api_key:
            raise ValueError("LLM_API_KEY 未设置，无法调用 LLM API")
        
        # 准备请求
        headers = self._prepare_headers()
        
        # 根据 API 基址 / 模型选择端点
        if self._use_anthropic_messages_api():
            endpoint = f"{self.api_base}/messages"
            payload = self._prepare_anthropic_payload(messages)
            parse_func = self._parse_anthropic_response
        else:
            endpoint = f"{self.api_base}/chat/completions"
            payload = self._prepare_openai_payload(messages, tools)
            parse_func = self._parse_openai_response
        
        # 发送请求（带 tools 时为整段非流式等待，推理模型首轮可能很久；与流式读超时对齐）
        req_timeout = self._request_timeout_seconds(messages)
        if tools:
            req_timeout = max(req_timeout, config.LLM_STREAM_READ_TIMEOUT)
        logger.debug(
            f"调用 LLM API (with context and tracking): {endpoint}, 模型: {self.model_name}, "
            f"timeout={req_timeout}s"
        )
        logger.debug(f"消息数量: {len(messages)}")
        
        try:
            response = requests.post(
                endpoint,
                headers=headers,
                json=payload,
                timeout=req_timeout
            )
            response.raise_for_status()
            
            response_data = response.json()
            logger.debug(f"LLM API 响应: {response.status_code}")
            
            llm_response = parse_func(response_data)
            
            # 保存token使用量
            if llm_response.usage:
                self._save_token_usage_async(llm_response.usage, platform)
            
            thinking_content = self._extract_thinking_content(response_data)
            llm_response.thinking = thinking_content
            
            return llm_response
            
        except requests.exceptions.Timeout:
            mm = messages_contain_multimodal_images(messages)
            logger.error(
                "LLM API 调用超时: %s秒%s",
                req_timeout,
                "（请求中含多模态图片）" if mm else "（无多模态图片，多为上下文过大或上游慢）",
            )
            raise
        except requests.exceptions.RequestException as e:
            logger.error(
                "LLM API(tracking) 失败 endpoint=%s model=%s: %s",
                endpoint,
                self.model_name,
                exc_detail(e),
            )
            if hasattr(e, 'response') and e.response is not None:
                logger.error(
                    "HTTP 响应 status=%s body_prefix=%r",
                    e.response.status_code,
                    (e.response.text or "")[:1200],
                )
            raise

    def generate_stream(
        self,
        messages: List[Dict[str, Any]],
        platform: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Generator[Tuple[str, str], None, Dict[str, Any]]:
        """
        流式生成（仅 OpenAI 兼容 `chat/completions` + SSE）。

        yield ``("thinking", chunk)``（delta 中 `reasoning_content` / `reasoning` / `thinking`，
        或仅在末包 ``choices[0].message`` 中给出整段推理时补一次）或 ``("content", chunk)``。

        生成器返回值为
        ``{"content": str, "thinking": Optional[str], "usage": Optional[dict]}``。

        Anthropic 路径：整段 ``generate_with_context_and_tracking`` 后 yield 一次
        ``("content", text)``，结构相同。
        """
        if not self.api_key:
            raise ValueError("LLM_API_KEY 未设置，无法调用 LLM API")

        if self._use_anthropic_messages_api():
            llm_resp = self.generate_with_context_and_tracking(
                messages, platform=platform
            )
            text = llm_resp.content
            th = llm_resp.thinking
            if text:
                yield ("content", text)
            return {
                "content": text or "",
                "thinking": (th.strip() if isinstance(th, str) and th.strip() else None),
                "usage": None,
            }

        headers = self._prepare_headers()
        endpoint = f"{self.api_base}/chat/completions"
        payload = self._prepare_openai_payload(messages, tools)
        payload["stream"] = True
        req_timeout = self._request_timeout_seconds(messages)
        # 流式：timeout 元组 (连接, 读) —— 读超时指「两次 SSE 片段之间」最长等待，须大于推理间隙
        stream_read = config.LLM_STREAM_READ_TIMEOUT
        stream_connect = min(30, stream_read)
        logger.debug(
            "调用 LLM API (stream): %s, 模型: %s, timeout=(connect=%ss, read=%ss)",
            endpoint,
            self.model_name,
            stream_connect,
            stream_read,
        )

        full_content: List[str] = []
        full_thinking: List[str] = []
        usage_out: Optional[Dict[str, int]] = None
        tc_by_index: Dict[int, Dict[str, str]] = {}

        def _merge_tool_call_delta(tc: Dict[str, Any]) -> None:
            if not isinstance(tc, dict):
                return
            try:
                idx = int(tc.get("index", 0) or 0)
            except (TypeError, ValueError):
                idx = 0
            slot = tc_by_index.setdefault(
                idx, {"id": "", "name": "", "arguments": ""}
            )
            tid = tc.get("id")
            if tid:
                slot["id"] = str(tid)
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            nm = fn.get("name")
            if nm:
                slot["name"] = str(nm)
            arg = fn.get("arguments")
            if arg is not None and arg != "":
                slot["arguments"] = (slot["arguments"] or "") + (
                    arg if isinstance(arg, str) else str(arg)
                )

        def _delta_thinking_piece(d: Dict[str, Any]) -> Optional[str]:
            for key in ("reasoning_content", "reasoning", "thinking"):
                v = d.get(key)
                if v is None or v == "":
                    continue
                return v if isinstance(v, str) else str(v)
            return None

        try:
            with requests.post(
                endpoint,
                headers=headers,
                json=payload,
                stream=True,
                timeout=(stream_connect, stream_read),
            ) as resp:
                if resp.status_code >= 400:
                    body_prev = (resp.text or "")[:1200]
                    logger.error(
                        "上游 chat/completions 非 2xx: status=%s endpoint=%s body_prefix=%r",
                        resp.status_code,
                        endpoint,
                        body_prev,
                    )
                    resp.raise_for_status()
                for raw_line in resp.iter_lines(decode_unicode=False):
                    if raw_line is None:
                        continue
                    try:
                        line = raw_line.decode("utf-8").strip()
                    except UnicodeDecodeError:
                        line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line or line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj: Dict[str, Any] = json.loads(data)
                    except json.JSONDecodeError:
                        logger.debug("流式跳过无法解析的行: %s", data[:200])
                        continue

                    u = obj.get("usage")
                    if isinstance(u, dict):
                        usage_out = u

                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    choice0 = choices[0] if isinstance(choices[0], dict) else {}
                    delta = choice0.get("delta")
                    if not isinstance(delta, dict):
                        delta = {}

                    th_piece = _delta_thinking_piece(delta)
                    if th_piece:
                        full_thinking.append(th_piece)
                        yield ("thinking", th_piece)

                    piece = delta.get("content")
                    if piece:
                        s = piece if isinstance(piece, str) else str(piece)
                        full_content.append(s)
                        yield ("content", s)

                    for tc in delta.get("tool_calls") or []:
                        _merge_tool_call_delta(tc)

                    # 部分网关只在最后一个 chunk 的 choices[0].message 里给整段推理，delta 无流式片段
                    msg = choice0.get("message")
                    if isinstance(msg, dict) and not full_thinking:
                        th_msg = _delta_thinking_piece(msg)
                        if th_msg:
                            full_thinking.append(th_msg)
                            yield ("thinking", th_msg)

        except requests.exceptions.RequestException as e:
            logger.error(
                "LLM 流式异常（建连或读 SSE 中断，常见 SSL/代理/上游断开） "
                "endpoint=%s model=%s timeout=(%s,%s)s: %s",
                endpoint,
                self.model_name,
                stream_connect,
                stream_read,
                exc_detail(e),
            )
            raise

        content_str = "".join(full_content)
        thinking_str = "".join(full_thinking).strip() or None

        tool_calls_out: Optional[List[Dict[str, Any]]] = None
        if tc_by_index:
            built: List[Dict[str, Any]] = []
            for idx in sorted(tc_by_index.keys()):
                slot = tc_by_index[idx]
                if not (slot.get("name") or "").strip() and not (
                    slot.get("id") or ""
                ).strip():
                    continue
                built.append(
                    {
                        "id": slot.get("id") or "",
                        "name": (slot.get("name") or "").strip(),
                        "arguments": slot.get("arguments") or "{}",
                    }
                )
            if built:
                tool_calls_out = built

        if usage_out:
            self._save_token_usage_async(usage_out, platform)

        return {
            "content": content_str,
            "thinking": thinking_str,
            "usage": usage_out,
            "tool_calls": tool_calls_out,
        }
    
    def generate_with_thinking(
        self, 
        prompt: str, 
        system_prompt: Optional[str] = None,
        conversation_history: Optional[List[Dict[str, str]]] = None,
        platform: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[str, Optional[str]]:
        """
        生成文本响应，提取思维链内容。
        
        Args:
            prompt: 用户提示
            system_prompt: 系统提示，用于指导模型行为
            conversation_history: 对话历史，格式为 [{"role": "user", "content": "..."}, ...]
            platform: 平台标识（可选）
            
        Returns:
            Tuple[str, Optional[str]]: (模型生成的文本内容, 思维链内容)
            
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
        
        # 根据 API 基址 / 模型选择端点
        if self._use_anthropic_messages_api():
            endpoint = f"{self.api_base}/messages"
            payload = self._prepare_anthropic_payload(messages)
            parse_func = self._parse_anthropic_response
        else:
            endpoint = f"{self.api_base}/chat/completions"
            payload = self._prepare_openai_payload(messages, tools)
            parse_func = self._parse_openai_response
        
        # 发送请求
        logger.debug(f"调用 LLM API (with thinking): {endpoint}, 模型: {self.model_name}")
        
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
            
            # 保存token使用量
            if llm_response.usage:
                self._save_token_usage_async(llm_response.usage, platform)
            
            # 提取思维链内容
            thinking_content = self._extract_thinking_content(response_data)
            
            return llm_response.content, thinking_content
            
        except requests.exceptions.Timeout:
            logger.error(f"LLM API 调用超时: {self.timeout}秒")
            raise
        except requests.exceptions.RequestException as e:
            logger.error(
                "LLM API(with thinking) 失败 endpoint=%s model=%s: %s",
                endpoint,
                self.model_name,
                exc_detail(e),
            )
            if hasattr(e, 'response') and e.response is not None:
                logger.error(
                    "HTTP 响应 status=%s body_prefix=%r",
                    e.response.status_code,
                    (e.response.text or "")[:1200],
                )
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