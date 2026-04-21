import re

with open('memory/context_builder.py', 'r', encoding='utf-8') as f:
    content = f.read()

content = content.replace(
    'recent_messages_section = await self._build_recent_messages_section(session_id)',
    'recent_messages_section = await self._build_recent_messages_section(session_id, exclude_message_id)'
)

content = content.replace(
    'async def _build_recent_messages_section(self, session_id: str) -> List[Dict[str, Any]]:',
    'async def _build_recent_messages_section(self, session_id: str, exclude_message_id: Optional[int] = None) -> List[Dict[str, Any]]:'
)

old_loop = '''            for msg in recent_messages:
                role = "user" if msg['role'] == "user" else "assistant"'''
new_loop = '''            for msg in recent_messages:
                if exclude_message_id and msg.get("id") == exclude_message_id:
                    continue
                role = "user" if msg['role'] == "user" else "assistant"'''

content = content.replace(old_loop, new_loop)

# Also update the standalone function `build_context`
standalone_call_old = '''    return await builder.build_context(
        session_id,
        user_message,
        images=images,
        llm_user_text=llm_user_text,
        telegram_segment_hint=telegram_segment_hint,
        tool_oral_coaching=tool_oral_coaching,
    )'''
standalone_call_new = '''    return await builder.build_context(
        session_id,
        user_message,
        images=images,
        llm_user_text=llm_user_text,
        telegram_segment_hint=telegram_segment_hint,
        tool_oral_coaching=tool_oral_coaching,
        exclude_message_id=exclude_message_id,
    )'''
content = content.replace(standalone_call_old, standalone_call_new)

with open('memory/context_builder.py', 'w', encoding='utf-8') as f:
    f.write(content)
