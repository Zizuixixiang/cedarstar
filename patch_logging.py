import re
import os

def patch_file(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # ensure imports exist
    if 'import traceback' not in content:
        content = re.sub(r'import logging\n', 'import logging\nimport traceback\n', content, count=1)
        if 'import traceback' not in content:
            # fallback
            content = "import traceback\n" + content
            if 'import logging' not in content:
                content = "import logging\n" + content
    
    patterns = [
        (r'(\s*)(.*await bot\.send_message\()', r'\1logging.debug(f"send called from: {\'\'.join(traceback.format_stack())}")\1\2'),
        (r'(\s*)(.*await telegram_bot\.send_message\()', r'\1logging.debug(f"send called from: {\'\'.join(traceback.format_stack())}")\1\2'),
        (r'(\s*)(.*await update\.message\.reply_text\()', r'\1logging.debug(f"send called from: {\'\'.join(traceback.format_stack())}")\1\2'),
        (r'(\s*)(.*await base_message\.reply_text\()', r'\1logging.debug(f"send called from: {\'\'.join(traceback.format_stack())}")\1\2'),
        (r'(\s*)(.*await ctx\.send\()', r'\1logging.debug(f"send called from: {\'\'.join(traceback.format_stack())}")\1\2'),
        (r'(\s*)(.*await base_message\.channel\.send\()', r'\1logging.debug(f"send called from: {\'\'.join(traceback.format_stack())}")\1\2')
    ]
    
    orig = content
    for p, repl in patterns:
        content = re.sub(p, repl, content)
        
    if orig != content:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"Patched {filepath}")
    else:
        print(f"No changes for {filepath}")

patch_file('bot/telegram_bot.py')
patch_file('bot/discord_bot.py')
patch_file('bot/telegram_notify.py')

