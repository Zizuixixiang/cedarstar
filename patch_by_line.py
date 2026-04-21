import sys

def patch(filepath, lines_to_patch):
    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    # check import
    has_traceback = any('import traceback' in line for line in lines)
    if not has_traceback:
        for i, line in enumerate(lines):
            if 'import logging' in line:
                lines.insert(i+1, "import traceback\n")
                # shift line numbers
                lines_to_patch = [l + 1 for l in lines_to_patch]
                break
    
    # patch backwards to avoid shifting issues
    lines_to_patch.sort(reverse=True)
    for l in lines_to_patch:
        idx = l - 1
        indent = len(lines[idx]) - len(lines[idx].lstrip())
        spaces = ' ' * indent
        lines.insert(idx, f'{spaces}stack = "".join(traceback.format_stack())\n{spaces}logging.debug(f"send called from: {{stack}}")\n')
        
    with open(filepath, 'w', encoding='utf-8') as f:
        f.writelines(lines)
    print(f"Patched {filepath}")

patch('bot/telegram_bot.py', [915, 933, 1047, 1224, 1340, 1362, 1668, 2167, 2263])
patch('bot/discord_bot.py', [389, 392])
patch('bot/telegram_notify.py', [48])
