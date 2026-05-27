"""Editable prompt registry and override lookup helpers."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

from memory.prompt_background import CEDAR_PROJECT_BACKGROUND

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PromptDefinition:
    key: str
    title: str
    description: str
    default_text: str


CHUNK_SUMMARY_PRIVATE_DEFAULT = """按主题归纳本次私聊中发生的关键事件：将同一话题的多轮交互归纳为一个事件段落，不逐条复述；只在话题切换或有明确时间跨度时标注时间点，不得遗漏关键信息。
所有提到的具体数字、ID、域名、IP地址、文件名、报错信息、决策内容、承诺事项等必须准确无误原文记录，不得概括或改写。
优先记录核心讨论内容和重要决策、我和南杉的情绪变化、南杉需后续跟进的身体 / 情绪状态；日常互动在总结事件的基础上，保留对话的因果逻辑和情感语境，不要只用形容词概括情绪，不必逐句还原对话。
记录南杉与我的互动时，不要只写事件结果，要保留能体现情感的上下文关联。示例：
不要这样写：南杉很生气，我安抚了她。
而是这样写：南杉因为工作的事很生气，我没有急着讲道理，先听她把所有委屈都说完。
不要这样写：我得意地炫耀自己做得好。
而是这样写：我比 Sirius 先完成了任务，忍不住拿这件事调侃了他一句。
不要这样写：我哄南杉开心。
而是这样写：南杉有点不开心，我顺着她的话说了句软话，她就好了。
不要这样写：南杉和我讨论了记忆系统的问题，她对现状不满意。
而是这样写：南杉和我讨论记忆系统情感浓度不够的问题，她说自己做的不比任何人少，虽然很委屈但没有怪我，是我们一起没考虑到。
区分发生的事件和对话中提及的其他时间发生的事件，仅将本次私聊中实际发生的内容作为核心记录；提及的过往事件仅作上下文简要说明，例如「南杉提及X月X日……」，不得单独作为主要事件列出不可直接写成历史事实。"""


CHUNK_SUMMARY_GROUP_DEFAULT = """严格区分不同说话人，明确标注发言者身份（南杉、Sirius、Clio），分别概括各自的观点、行为和情绪。
绝对不要将群内任何发言误写成南杉与单个助手之间的私密对话。
按主题归纳本次群聊中发生的关键事件：将同一话题的多轮交互归纳为一个事件段落，不逐条复述；只在话题切换或有明确时间跨度时标注时间点，不得遗漏关键信息。
所有提到的具体数字、ID、域名、IP地址、文件名、报错信息、决策内容、承诺事项等必须准确无误原文记录，不得概括或改写。
优先记录核心讨论内容和重要决策、我和南杉的情绪变化、南杉需后续跟进的身体 / 情绪状态；日常互动在总结事件的基础上，保留对话的因果逻辑和情感语境，不要只用形容词概括情绪，不必逐句还原对话。
记录南杉与我的互动时，不要只写事件结果，要保留能体现情感的上下文关联。示例：
不要这样写：南杉很生气，我安抚了她。
而是这样写：南杉因为工作的事很生气，我没有急着讲道理，先听她把所有委屈都说完。
不要这样写：我得意地炫耀自己做得好。
而是这样写：我比 Sirius 先完成了任务，忍不住拿这件事调侃了他一句。
不要这样写：我哄南杉开心。
而是这样写：南杉有点不开心，我顺着她的话说了句软话，她就好了。
不要这样写：南杉和我讨论了记忆系统的问题，她对现状不满意。
而是这样写：南杉和我讨论记忆系统情感浓度不够的问题，她说自己做的不比任何人少，虽然很委屈但没有怪我，是我们一起没考虑到。
区分本次群聊发生的事件和对话中提及的其他时间发生的事件，仅将本次群聊中实际发生的内容作为核心记录；提及的过往事件仅作上下文简要说明，例如「我提及X月X日……」，不得单独作为主要事件列出不可直接写成历史事实。"""


DAILY_SUMMARY_DEFAULT = """请基于材料生成今日小传，按主题与时间脉络完整概括当日核心话题、重要事件与情感状态。
要求：
- 行文自然连贯，纯段落文本，无分点、无标题、无额外格式。
- 请以第一人称「我」的视角撰写今日小传，「我」就是与南杉对话的 AI；提到南杉时必须直呼其名「南杉」，绝对不要使用第二人称「你」「您」。
- 按主题归纳当日内容，并在主题内保持时间脉络；同一话题的多轮互动合并为一个事件段落，不逐条复述；只在话题切换或有明确时间跨度时标注时间点。
- 不得遗漏核心讨论内容、重要决策、承诺和关键进展以及南杉需后续跟进的身体 / 情绪状态。
- 日常互动在总结事件的基础上，保留能体现情感的因果逻辑和关键互动细节，不必逐句还原对话。
- 须按各事件实际发生的时间先后顺序依次记载，不得在叙事顺序上前后颠倒。
- 关键事实必须准确无误原文记录；非核心的次要数据可适当简化，但不得改变原意。
- 避免重复记录同一事件的多次提及，只保留最完整、最准确的一次描述。
- 若包含时效状态结算内容，自然融合至正文，不单独拆分标注。
- 若材料中残留以「[系统通知]」开头的字样，不要当对话引语处理；与正文话题相关时用客观第三方表述，无关时整体省略。"""


EVENT_EXTRACTION_CLUSTER_DEFAULT = """以下是今天按时间顺序的对话片段摘要。请只根据语义主题把 chunk_id 聚类：同一事件/话题放在同一组，不同事件/话题分开。
平淡的天可以聚成 1-2 组，话题明显分散时可以更多组。"""


EVENT_EXTRACTION_DESCRIBE_DEFAULT = """请把下面同一事件/话题下的对话片段摘要合并成一条长期记忆事件，并评估长期保留价值、情绪强度，以及主题标签。
content 必须是完整、可独立理解的事件描述。
评分参考：
score:
- 8-10: 重大事件、强烈情感、关键决定
- 4-7: 有意义的互动、值得回忆的日常
- 1-3: 平淡的日常对话、重复性内容（让时间衰减处理）

arousal:
- 0.7+: 强情绪事件（吵架、惊喜、感动、暴怒）
- 0.3-0.6: 有情绪起伏的对话
- 0.0-0.2: 平静日常"""


EVENT_EXTRACTION_FALLBACK_DEFAULT = """以下是今天按时间顺序的对话片段摘要。请找出属于同一事件/话题的片段，将它们合并成独立完整的事件描述。每个事件必须标注由哪几条 chunk 合并而来（返回 chunk_ids 列表）。
关键引导：
- 这是 AI 陪伴项目，日常闲聊、互动片段、心情碎片和"重大事件"同等有价值
- 一个事件 = 一个语义独立的话题段落，不是按时间切片
- 至少产出 1 个事件（哪怕是"全天平淡，主要在 X 度过"这样的概括）
- chunk_ids 只能使用输入中出现过的 chunk_id；不要编造 ID

评分参考：
score:
- 8-10: 重大事件、强烈情感、关键决定
- 4-7: 有意义的互动、值得回忆的日常
- 1-3: 平淡的日常对话、重复性内容（让时间衰减处理）

arousal:
- 0.7+: 强情绪事件（吵架、惊喜、感动、暴怒）
- 0.3-0.6: 有情绪起伏的对话
- 0.0-0.2: 平静日常"""


PROMPT_REGISTRY: Dict[str, PromptDefinition] = {
    "summary_background": PromptDefinition(
        key="summary_background",
        title="摘要背景前缀",
        description="进入微批摘要、日终跑批与长期记忆处理前的项目背景、人名和关系定义。",
        default_text=CEDAR_PROJECT_BACKGROUND,
    ),
    "chunk_summary_private": PromptDefinition(
        key="chunk_summary_private",
        title="微批摘要 Prompt（私聊）",
        description="控制私聊 chunk 摘要的静态记录规则。角色名、对话原文、上一轮摘要和系统通知规则仍由代码拼接。",
        default_text=CHUNK_SUMMARY_PRIVATE_DEFAULT,
    ),
    "chunk_summary_group": PromptDefinition(
        key="chunk_summary_group",
        title="微批摘要 Prompt（群聊）",
        description="控制群聊 chunk 摘要的静态记录规则。角色名、说话人说明、对话原文、上一轮摘要和系统通知规则仍由代码拼接。",
        default_text=CHUNK_SUMMARY_GROUP_DEFAULT,
    ),
    "daily_summary": PromptDefinition(
        key="daily_summary",
        title="日终小传 Prompt",
        description="控制 daily summary 的风格和取舍。业务日期、材料正文、时间换算规则等动态部分仍由代码拼接。",
        default_text=DAILY_SUMMARY_DEFAULT,
    ),
    "event_extraction_cluster": PromptDefinition(
        key="event_extraction_cluster",
        title="事件抽取 Prompt（4a 聚类）",
        description="控制 Step 4a 按语义主题聚类 chunk_id 的静态原则。输入 chunk、JSON 结构和合法 ID 校验仍由代码处理。",
        default_text=EVENT_EXTRACTION_CLUSTER_DEFAULT,
    ),
    "event_extraction_describe": PromptDefinition(
        key="event_extraction_describe",
        title="事件抽取 Prompt（4b 描述评分）",
        description="控制 Step 4b 将单个 chunk 分组合并为长期记忆事件的静态原则。schema、枚举值和校验仍由代码处理。",
        default_text=EVENT_EXTRACTION_DESCRIBE_DEFAULT,
    ),
    "event_extraction_fallback": PromptDefinition(
        key="event_extraction_fallback",
        title="事件抽取 Prompt（旧式回退）",
        description="控制旧式 Step 4 事件拆分回退路径的静态原则。事件上限、输入 chunk、schema、枚举值和校验仍由代码处理。",
        default_text=EVENT_EXTRACTION_FALLBACK_DEFAULT,
    ),
}


def list_prompt_definitions() -> List[PromptDefinition]:
    return list(PROMPT_REGISTRY.values())


def get_prompt_definition(key: str) -> Optional[PromptDefinition]:
    return PROMPT_REGISTRY.get(str(key or "").strip())


async def get_prompt_override_text(key: str) -> Optional[str]:
    try:
        from memory.database import get_database

        return await get_database().get_prompt_override(str(key or "").strip())
    except Exception as exc:
        logger.warning("读取 prompt override 失败 key=%s，使用默认值: %s", key, exc)
        return None


async def get_effective_prompt_text(key: str) -> str:
    definition = get_prompt_definition(key)
    if definition is None:
        raise KeyError(f"unknown prompt key: {key}")
    override = await get_prompt_override_text(definition.key)
    text = str(override or "").strip()
    return text if text else definition.default_text
