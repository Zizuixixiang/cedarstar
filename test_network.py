#!/usr/bin/env python3
"""
测试网络连接，检查是否能访问 Discord API。
"""

import asyncio
import aiohttp
import time

async def test_discord_connection():
    """测试 Discord API 连接"""
    print("测试 Discord API 连接...")
    
    try:
        async with aiohttp.ClientSession() as session:
            start_time = time.time()
            
            # 尝试连接 Discord API
            async with session.get('https://discord.com/api/v10/users/@me', timeout=10) as response:
                elapsed = time.time() - start_time
                
                if response.status == 200:
                    print(f"✓ Discord API 连接成功！响应时间: {elapsed:.2f}秒")
                    return True
                else:
                    print(f"✗ Discord API 返回错误状态码: {response.status}")
                    return False
                    
    except asyncio.TimeoutError:
        print("✗ Discord API 连接超时（10秒）")
        return False
    except aiohttp.ClientConnectorError as e:
        print(f"✗ Discord API 连接错误: {e}")
        return False
    except Exception as e:
        print(f"✗ 未知错误: {e}")
        return False

async def test_google_connection():
    """测试 Google 连接（作为网络连通性参考）"""
    print("\n测试 Google 连接...")
    
    try:
        async with aiohttp.ClientSession() as session:
            start_time = time.time()
            
            async with session.get('https://www.google.com', timeout=10) as response:
                elapsed = time.time() - start_time
                
                if response.status == 200:
                    print(f"✓ Google 连接成功！响应时间: {elapsed:.2f}秒")
                    return True
                else:
                    print(f"✗ Google 返回错误状态码: {response.status}")
                    return False
                    
    except asyncio.TimeoutError:
        print("✗ Google 连接超时（10秒）")
        return False
    except Exception as e:
        print(f"✗ 未知错误: {e}")
        return False

async def main():
    """主函数"""
    print("网络连接测试开始...")
    print("=" * 50)
    
    # 测试 Google 连接
    google_ok = await test_google_connection()
    
    # 测试 Discord 连接
    discord_ok = await test_discord_connection()
    
    print("\n" + "=" * 50)
    print("测试结果总结:")
    print(f"Google 连接: {'✓ 成功' if google_ok else '✗ 失败'}")
    print(f"Discord 连接: {'✓ 成功' if discord_ok else '✗ 失败'}")
    
    if google_ok and not discord_ok:
        print("\n⚠️ 网络可以访问互联网，但无法连接 Discord")
        print("可能的原因:")
        print("1. Discord API 被防火墙或网络策略阻止")
        print("2. 需要配置代理服务器")
        print("3. Discord 服务暂时不可用")
    elif not google_ok:
        print("\n⚠️ 网络无法访问互联网")
        print("请检查网络连接和代理设置")
    else:
        print("\n✅ 网络连接正常")

if __name__ == "__main__":
    asyncio.run(main())