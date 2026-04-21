import re

files = ['bot/telegram_bot.py', 'bot/discord_bot.py', 'bot/telegram_notify.py']
for filepath in files:
    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    for i, line in enumerate(lines):
        if re.search(r'await (bot|telegram_bot|ctx|update\.message|base_message|base_message\.channel)\.(send_message|send|reply_text|edit_message_text)\(', line):
            print(f"{filepath}:{i+1}: {line.strip()}")
