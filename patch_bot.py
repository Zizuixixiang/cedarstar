import re

def patch(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    # Find where build_context is called
    content = content.replace(
        'tool_oral_coaching=tool_oral_coaching,\n            )',
        'tool_oral_coaching=tool_oral_coaching,\n                exclude_message_id=user_row_id if \'user_row_id\' in locals() else None,\n            )'
    )

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
        
patch('bot/telegram_bot.py')
patch('bot/discord_bot.py')
