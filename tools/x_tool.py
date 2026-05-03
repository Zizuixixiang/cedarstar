"""
OpenAI function calling：X (Twitter) 全功能工具集。

依赖 tweepy（OAuth 1.0a 读写权限），所有操作共享每日配额（get_user 除外）。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 每日配额：内存 + DB 持久化（config key: x_usage_YYYY-MM-DD）
# ---------------------------------------------------------------------------
_daily_usage: Dict[str, int] = {}


def _today_key() -> str:
    return date.today().isoformat()


def _usage_config_key() -> str:
    return f"x_usage_{_today_key()}"


def _get_today_count() -> int:
    return _daily_usage.get(_today_key(), 0)


async def _sync_today_from_db() -> None:
    """启动或内存为 0 时从 DB 同步当日用量。"""
    k = _today_key()
    if _daily_usage.get(k):
        return
    try:
        from memory.database import get_database
        db = get_database()
        raw = await db.get_config(_usage_config_key(), "0")
        _daily_usage[k] = max(0, int(raw))
    except Exception:
        pass


async def _inc_today_count(n: int = 1) -> None:
    k = _today_key()
    _daily_usage[k] = _daily_usage.get(k, 0) + n
    try:
        from memory.database import get_database
        db = get_database()
        await db.set_config(_usage_config_key(), str(_daily_usage[k]))
    except Exception:
        pass


async def _check_quota() -> Optional[Dict[str, Any]]:
    """检查配额，超限返回 error dict，未超限返回 None。"""
    await _sync_today_from_db()
    limit = await _get_daily_limit()
    if _get_today_count() >= limit:
        return {"success": False, "error": "daily_limit_exceeded", "limit": limit}
    return None


async def _quota_info() -> Dict[str, int]:
    await _sync_today_from_db()
    return {"used_today": _get_today_count()}


# ---------------------------------------------------------------------------
# tweepy 客户端（惰性初始化）
# ---------------------------------------------------------------------------
_client = None


def _get_client():
    global _client
    if _client is not None:
        return _client
    try:
        import tweepy
    except ImportError:
        logger.error("tweepy 未安装，请 pip install tweepy")
        return None
    bearer_token = os.getenv("X_BEARER_TOKEN", "")
    consumer_key = os.getenv("X_CONSUMER_KEY", "")
    consumer_secret = os.getenv("X_CONSUMER_SECRET", "")
    access_token = os.getenv("X_ACCESS_TOKEN", "")
    access_token_secret = os.getenv("X_ACCESS_TOKEN_SECRET", "")
    if not all([consumer_key, consumer_secret, access_token, access_token_secret]):
        logger.error("X OAuth 凭证未完整配置")
        return None
    try:
        _client = tweepy.Client(
            bearer_token=bearer_token or None,
            consumer_key=consumer_key,
            consumer_secret=consumer_secret,
            access_token=access_token,
            access_token_secret=access_token_secret,
        )
    except Exception as e:
        logger.error("tweepy.Client 初始化失败: %s", e)
        return None
    return _client


async def _get_daily_limit() -> int:
    default = 100
    try:
        from memory.database import get_database
        db = get_database()
        raw = await db.get_config("x_daily_read_limit", str(default))
        return max(1, int(raw))
    except Exception:
        return default


def _my_user_id() -> Optional[int]:
    """获取当前认证用户 ID（缓存在 client.get_me 结果中）。"""
    client = _get_client()
    if client is None:
        return None
    try:
        me = client.get_me()
        return me.data.id if me.data else None
    except Exception as e:
        logger.warning("获取当前用户 ID 失败: %s", e)
        return None


# ---------------------------------------------------------------------------
# 1. post_tweet
# ---------------------------------------------------------------------------
async def post_tweet(text: str) -> Dict[str, Any]:
    client = _get_client()
    if client is None:
        return {"success": False, "error": "X 客户端未初始化"}
    t = (text or "").strip()
    if not t:
        return {"success": False, "error": "推文内容不能为空"}
    qerr = await _check_quota()
    if qerr:
        return qerr
    try:
        resp = client.create_tweet(text=t)
        tweet_id = str(resp.data["id"])
        try:
            me = client.get_me()
            username = me.data.username
        except Exception:
            username = "i"
        url = f"https://x.com/{username}/status/{tweet_id}"
        await _inc_today_count()
        return {"success": True, "tweet_id": tweet_id, "url": url, **(await _quota_info())}
    except Exception as e:
        logger.warning("post_tweet 失败: %s", e)
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# 2. read_mentions
# ---------------------------------------------------------------------------
async def read_mentions(max_results: int = 10) -> Dict[str, Any]:
    limit = await _get_daily_limit()
    used = _get_today_count()
    if used >= limit:
        return {"success": False, "error": "daily_limit_exceeded", "limit": limit}
    client = _get_client()
    if client is None:
        return {"success": False, "error": "X 客户端未初始化"}
    remaining = limit - used
    n = max(1, min(max_results, remaining))
    try:
        user_id = _my_user_id()
        if user_id is None:
            return {"success": False, "error": "无法获取当前用户信息"}
        resp = client.get_users_mentions(
            id=user_id, max_results=n,
            tweet_fields=["created_at", "author_id", "text"],
        )
        tweets = []
        if resp.data:
            for tw in resp.data:
                tweets.append({
                    "id": str(tw.id), "text": tw.text,
                    "author_id": str(tw.author_id) if tw.author_id else None,
                    "created_at": tw.created_at.isoformat() if tw.created_at else None,
                })
        await _inc_today_count(len(tweets))
        return {"success": True, "tweets": tweets, "limit": limit, **(await _quota_info())}
    except Exception as e:
        logger.warning("read_mentions 失败: %s", e)
        return {"success": False, "error": str(e), "limit": limit}


# ---------------------------------------------------------------------------
# 3. like_tweet
# ---------------------------------------------------------------------------
async def like_tweet(tweet_id: str) -> Dict[str, Any]:
    client = _get_client()
    if client is None:
        return {"success": False, "error": "X 客户端未初始化"}
    tid = (tweet_id or "").strip()
    if not tid:
        return {"success": False, "error": "tweet_id 不能为空"}
    qerr = await _check_quota()
    if qerr:
        return qerr
    try:
        user_id = _my_user_id()
        if user_id is None:
            return {"success": False, "error": "无法获取当前用户信息"}
        client.like(tweet_id=int(tid))
        await _inc_today_count()
        return {"success": True, "tweet_id": tid, **(await _quota_info())}
    except Exception as e:
        logger.warning("like_tweet 失败: %s", e)
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# 4. unlike_tweet
# ---------------------------------------------------------------------------
async def unlike_tweet(tweet_id: str) -> Dict[str, Any]:
    client = _get_client()
    if client is None:
        return {"success": False, "error": "X 客户端未初始化"}
    tid = (tweet_id or "").strip()
    if not tid:
        return {"success": False, "error": "tweet_id 不能为空"}
    qerr = await _check_quota()
    if qerr:
        return qerr
    try:
        user_id = _my_user_id()
        if user_id is None:
            return {"success": False, "error": "无法获取当前用户信息"}
        client.unlike(tweet_id=int(tid))
        await _inc_today_count()
        return {"success": True, "tweet_id": tid, **(await _quota_info())}
    except Exception as e:
        logger.warning("unlike_tweet 失败: %s", e)
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# 5. reply_tweet
# ---------------------------------------------------------------------------
async def reply_tweet(tweet_id: str, text: str) -> Dict[str, Any]:
    client = _get_client()
    if client is None:
        return {"success": False, "error": "X 客户端未初始化"}
    tid = (tweet_id or "").strip()
    t = (text or "").strip()
    if not tid:
        return {"success": False, "error": "tweet_id 不能为空"}
    if not t:
        return {"success": False, "error": "回复内容不能为空"}
    qerr = await _check_quota()
    if qerr:
        return qerr
    try:
        resp = client.create_tweet(text=t, in_reply_to_tweet_id=int(tid))
        reply_id = str(resp.data["id"])
        await _inc_today_count()
        return {"success": True, "tweet_id": reply_id, "in_reply_to": tid, **(await _quota_info())}
    except Exception as e:
        logger.warning("reply_tweet 失败: %s", e)
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# 6. search_tweets
# ---------------------------------------------------------------------------
async def search_tweets(query: str, max_results: int = 10) -> Dict[str, Any]:
    client = _get_client()
    if client is None:
        return {"success": False, "error": "X 客户端未初始化"}
    q = (query or "").strip()
    if not q:
        return {"success": False, "error": "搜索关键词不能为空"}
    limit = await _get_daily_limit()
    used = _get_today_count()
    if used >= limit:
        return {"success": False, "error": "daily_limit_exceeded", "limit": limit}
    remaining = limit - used
    n = max(10, min(max_results, remaining))  # API 最小 10
    try:
        resp = client.search_recent_tweets(
            query=q, max_results=n,
            tweet_fields=["created_at", "author_id", "text"],
        )
        tweets = []
        if resp.data:
            for tw in resp.data:
                tweets.append({
                    "id": str(tw.id), "text": tw.text,
                    "author_id": str(tw.author_id) if tw.author_id else None,
                    "created_at": tw.created_at.isoformat() if tw.created_at else None,
                })
        await _inc_today_count(len(tweets))
        return {"success": True, "query": q, "tweets": tweets, "limit": limit, **(await _quota_info())}
    except Exception as e:
        logger.warning("search_tweets 失败: %s", e)
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# 7. get_timeline
# ---------------------------------------------------------------------------
async def get_timeline(max_results: int = 10) -> Dict[str, Any]:
    client = _get_client()
    if client is None:
        return {"success": False, "error": "X 客户端未初始化"}
    limit = await _get_daily_limit()
    used = _get_today_count()
    if used >= limit:
        return {"success": False, "error": "daily_limit_exceeded", "limit": limit}
    remaining = limit - used
    n = max(1, min(max_results, remaining))
    try:
        resp = client.get_home_timeline(
            max_results=n,
            tweet_fields=["created_at", "author_id", "text"],
        )
        tweets = []
        if resp.data:
            for tw in resp.data:
                tweets.append({
                    "id": str(tw.id), "text": tw.text,
                    "author_id": str(tw.author_id) if tw.author_id else None,
                    "created_at": tw.created_at.isoformat() if tw.created_at else None,
                })
        await _inc_today_count(len(tweets))
        return {"success": True, "tweets": tweets, "limit": limit, **(await _quota_info())}
    except Exception as e:
        logger.warning("get_timeline 失败: %s", e)
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# 8. get_user（不消耗配额）
# ---------------------------------------------------------------------------
async def get_user(username: str) -> Dict[str, Any]:
    client = _get_client()
    if client is None:
        return {"success": False, "error": "X 客户端未初始化"}
    uname = (username or "").strip().lstrip("@")
    if not uname:
        return {"success": False, "error": "用户名不能为空"}
    try:
        resp = client.get_user(
            username=uname,
            user_fields=["description", "public_metrics", "created_at", "profile_image_url"],
        )
        if not resp.data:
            return {"success": False, "error": f"用户 @{uname} 不存在"}
        u = resp.data
        metrics = u.public_metrics or {}
        return {
            "success": True,
            "user": {
                "id": str(u.id),
                "name": u.name,
                "username": u.username,
                "description": u.description or "",
                "followers_count": metrics.get("followers_count", 0),
                "following_count": metrics.get("following_count", 0),
                "tweet_count": metrics.get("tweet_count", 0),
                "profile_image_url": u.profile_image_url or "",
                "created_at": u.created_at.isoformat() if u.created_at else None,
            },
        }
    except Exception as e:
        logger.warning("get_user 失败: %s", e)
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# 9. follow_user
# ---------------------------------------------------------------------------
async def follow_user(user_id: str) -> Dict[str, Any]:
    client = _get_client()
    if client is None:
        return {"success": False, "error": "X 客户端未初始化"}
    uid = (user_id or "").strip()
    if not uid:
        return {"success": False, "error": "user_id 不能为空"}
    qerr = await _check_quota()
    if qerr:
        return qerr
    try:
        my_id = _my_user_id()
        if my_id is None:
            return {"success": False, "error": "无法获取当前用户信息"}
        client.follow_user(target_user_id=int(uid))
        await _inc_today_count()
        return {"success": True, "followed_user_id": uid, **(await _quota_info())}
    except Exception as e:
        logger.warning("follow_user 失败: %s", e)
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# 10. unfollow_user
# ---------------------------------------------------------------------------
async def unfollow_user(user_id: str) -> Dict[str, Any]:
    client = _get_client()
    if client is None:
        return {"success": False, "error": "X 客户端未初始化"}
    uid = (user_id or "").strip()
    if not uid:
        return {"success": False, "error": "user_id 不能为空"}
    qerr = await _check_quota()
    if qerr:
        return qerr
    try:
        my_id = _my_user_id()
        if my_id is None:
            return {"success": False, "error": "无法获取当前用户信息"}
        client.unfollow_user(target_user_id=int(uid))
        await _inc_today_count()
        return {"success": True, "unfollowed_user_id": uid, **(await _quota_info())}
    except Exception as e:
        logger.warning("unfollow_user 失败: %s", e)
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# 11. get_followers
# ---------------------------------------------------------------------------
async def get_followers(max_results: int = 20) -> Dict[str, Any]:
    client = _get_client()
    if client is None:
        return {"success": False, "error": "X 客户端未初始化"}
    limit = await _get_daily_limit()
    used = _get_today_count()
    if used >= limit:
        return {"success": False, "error": "daily_limit_exceeded", "limit": limit}
    remaining = limit - used
    n = max(1, min(max_results, remaining))
    try:
        my_id = _my_user_id()
        if my_id is None:
            return {"success": False, "error": "无法获取当前用户信息"}
        resp = client.get_users_followers(
            id=my_id, max_results=n,
            user_fields=["description", "public_metrics", "profile_image_url"],
        )
        users = []
        if resp.data:
            for u in resp.data:
                metrics = u.public_metrics or {}
                users.append({
                    "id": str(u.id), "name": u.name, "username": u.username,
                    "description": (u.description or "")[:100],
                    "followers_count": metrics.get("followers_count", 0),
                })
        await _inc_today_count(len(users))
        return {"success": True, "followers": users, "limit": limit, **(await _quota_info())}
    except Exception as e:
        logger.warning("get_followers 失败: %s", e)
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# 供外部查询当日用量（Mini App API 用）
# ---------------------------------------------------------------------------
async def get_today_usage() -> Dict[str, Any]:
    """返回 {used_today, limit, date}。"""
    await _sync_today_from_db()
    return {
        "used_today": _get_today_count(),
        "limit": await _get_daily_limit(),
        "date": _today_key(),
    }


# ---------------------------------------------------------------------------
# 工具执行入口（供 tool router 调用）
# ---------------------------------------------------------------------------
async def execute_x_function_call(function_name: str, arguments: Any) -> str:
    args: Dict[str, Any]
    if isinstance(arguments, str):
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            args = {}
    elif isinstance(arguments, dict):
        args = arguments
    else:
        args = {}

    dispatch = {
        "post_tweet": lambda: post_tweet(str(args.get("text") or "")),
        "read_mentions": lambda: read_mentions(int(args.get("max_results", 10))),
        "like_tweet": lambda: like_tweet(str(args.get("tweet_id") or "")),
        "unlike_tweet": lambda: unlike_tweet(str(args.get("tweet_id") or "")),
        "reply_tweet": lambda: reply_tweet(
            str(args.get("tweet_id") or ""), str(args.get("text") or "")
        ),
        "search_tweets": lambda: search_tweets(
            str(args.get("query") or ""), int(args.get("max_results", 10))
        ),
        "get_timeline": lambda: get_timeline(int(args.get("max_results", 10))),
        "get_user": lambda: get_user(str(args.get("username") or "")),
        "follow_user": lambda: follow_user(str(args.get("user_id") or "")),
        "unfollow_user": lambda: unfollow_user(str(args.get("user_id") or "")),
        "get_followers": lambda: get_followers(int(args.get("max_results", 20))),
    }

    fn = dispatch.get(function_name)
    try:
        result = await fn() if fn else {"success": False, "error": "未知工具"}
    except Exception as e:
        logger.warning("execute_x_function_call(%s) 失败: %s", function_name, e)
        result = {"success": False, "error": str(e)}
    return json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 工具描述（供 LLM 调用）
# ---------------------------------------------------------------------------
X_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "post_tweet",
            "description": "在 X (Twitter) 上发布一条推文",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "推文正文，最长 280 字符"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_mentions",
            "description": "读取当前用户在 X 上的最新 @提及",
            "parameters": {
                "type": "object",
                "properties": {
                    "max_results": {"type": "integer", "description": "最多返回条数，默认 10"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "like_tweet",
            "description": "点赞一条推文",
            "parameters": {
                "type": "object",
                "properties": {
                    "tweet_id": {"type": "string", "description": "要点赞的推文 ID"},
                },
                "required": ["tweet_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unlike_tweet",
            "description": "取消点赞一条推文",
            "parameters": {
                "type": "object",
                "properties": {
                    "tweet_id": {"type": "string", "description": "要取消赞的推文 ID"},
                },
                "required": ["tweet_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reply_tweet",
            "description": "回复一条推文",
            "parameters": {
                "type": "object",
                "properties": {
                    "tweet_id": {"type": "string", "description": "要回复的推文 ID"},
                    "text": {"type": "string", "description": "回复内容，最长 280 字符"},
                },
                "required": ["tweet_id", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_tweets",
            "description": "关键词搜索近 7 天的推文",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                    "max_results": {"type": "integer", "description": "最多返回条数，默认 10"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_timeline",
            "description": "读取自己关注的人的时间线",
            "parameters": {
                "type": "object",
                "properties": {
                    "max_results": {"type": "integer", "description": "最多返回条数，默认 10"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_user",
            "description": "查看 X 用户基本信息（不消耗配额）",
            "parameters": {
                "type": "object",
                "properties": {
                    "username": {"type": "string", "description": "用户名（不含@）"},
                },
                "required": ["username"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "follow_user",
            "description": "关注一个 X 用户",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "要关注的用户 ID"},
                },
                "required": ["user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unfollow_user",
            "description": "取消关注一个 X 用户",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "要取关的用户 ID"},
                },
                "required": ["user_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_followers",
            "description": "读取自己的粉丝列表",
            "parameters": {
                "type": "object",
                "properties": {
                    "max_results": {"type": "integer", "description": "最多返回条数，默认 20"},
                },
                "required": [],
            },
        },
    },
]
