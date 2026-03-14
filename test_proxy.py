#!/usr/bin/env python3
"""
测试代理连接。
"""

import os
import sys
import asyncio
import aiohttp
import time

# 添加当前目录到 Python 路径
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

from config import config, validate_config

async def test_with_proxy():
    """使用代理测试连接"""
    print("测试代理连接...")
    print(f"代理配置: {config.proxy_dict}")
    print(f"启用代理: {config.ENABLE_PROXY}")
    
    proxy_config = config.proxy_dict
    
    try:
        if proxy_config:
            print(f"\n使用代理测试 Discord API 连接...")
            print(f"代理: {proxy_config}")
            
            connector = aiohttp.TCPConnector(ssl=False)
            timeout = aiohttp.ClientTimeout(total=30)
            
            async with aiohttp.ClientSession(
                connector=connector,
                timeout=timeout
            ) as session:
                start_time = time.time()
                
                # 测试 Discord API
                try:
                    async with session.get(
                        'https://discord.com/api/v10/users/@me',
                        proxy=proxy_config.get('https') or proxy_config.get('http'),
                        timeout=10
                    ) as response:
                        elapsed = time.time() - start_time
                        
                        if response.status == 401:  # 401 表示连接成功但令牌无效
                            print(f"✓ Discord API 连接成功！响应时间: {elapsed:.2f}秒")
                            print("  注意: 返回 401 状态码，表示连接成功但令牌无效（这是正常的）")
                            return True
                        elif response.status == 200:
                            print(f"✓ Discord API 连接成功！响应时间: {elapsed:.2f}秒")
                            return True
                        else:
                            print(f"✗ Discord API 返回状态码: {response.status}")
                            return False
                except asyncio.TimeoutError:
                    print("✗ Discord API 连接超时（10秒）")
                    return False
                except aiohttp.ClientConnectorError as e:
                    print(f"✗ Discord API 连接错误: {e}")
                    return False
        else:
            print("✗ 未配置代理")
            return False
            
    except Exception as e:
        print(f"✗ 测试过程中出错: {e}")
        import traceback
        traceback.print_exc()
        return False

async def test_direct_connection():
    """测试直连（不使用代理）"""
    print("\n测试直连（不使用代理）...")
    
    try:
        async with aiohttp.ClientSession() as session:
            start_time = time.time()
            
            try:
                async with session.get('https://discord.com/api/v10/users/@me', timeout=10) as response:
                    elapsed = time.time() - start_time
                    
                    if response.status == 401 or response.status == 200:
                        print(f"✓ 直连成功！响应时间: {elapsed:.2f}秒")
                        return True
                    else:
                        print(f"✗ 直连返回状态码: {response.status}")
                        return False
            except asyncio.TimeoutError:
                print("✗ 直连接接超时（10秒）")
                return False
            except aiohttp.ClientConnectorError as e:
                print(f"✗ 直连接接错误: {e}")
                return False
                
    except Exception as e:
        print(f"✗ 直连测试过程中出错: {e}")
        return False

async def main():
    """主函数"""
    print("代理连接测试开始...")
    print("=" * 50)
    
    # 验证配置
    try:
        validate_config()
        print("✓ 配置验证通过")
    except ValueError as e:
        print(f"✗ 配置验证失败: {e}")
        return
    
    # 测试直连
    direct_ok = await test_direct_connection()
    
    # 测试代理连接
    proxy_ok = await test_with_proxy()
    
    print("\n" + "=" * 50)
    print("测试结果总结:")
    print(f"直连: {'✓ 成功' if direct_ok else '✗ 失败'}")
    print(f"代理连接: {'✓ 成功' if proxy_ok else '✗ 失败'}")
    
    if proxy_ok:
        print("\n✅ 代理配置正确，可以连接到 Discord API")
        print("现在可以启动 Discord 机器人了")
    elif direct_ok:
        print("\n⚠️ 直连成功但代理连接失败")
        print("建议: 可以尝试禁用代理或检查代理配置")
    else:
        print("\n❌ 所有连接方式都失败")
        print("请检查网络连接和代理设置")

if __name__ == "__main__":
    asyncio.run(main())