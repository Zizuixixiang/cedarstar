# 总开关：负责把所有组件串联起来启动

def main():
    from bot import discord_bot
    from llm import llm_interface
    from memory import memory_store
    from tools import tools_manager
    from services import services_integration

    # 初始化各个模块
    discord_bot.init_bot()
    llm_interface.init_llm()
    memory_store.init_memory()
    tools_manager.init_tools()
    services_integration.init_services()

    # 启动所有组件
    discord_bot.run_bot()

if __name__ == "__main__":
    main()