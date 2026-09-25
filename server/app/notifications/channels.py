import base64
import hashlib
import hmac
import time
import urllib.parse
from typing import Any

import httpx

from app.core.redaction import redact_sensitive

TIMEOUT = 8.0


def send_bark(config: dict[str, Any], title: str, content: str) -> tuple[bool, str]:
    key = config.get("key", "").strip()
    if not key:
        return False, "Bark Key 不能为空"
    server = (str(config.get("server") or "")).strip().rstrip("/") or "https://api.day.app"
    url = f"{server}/{key}"
    payload = {
        "title": title,
        "body": content,
        "group": config.get("group", "信用卡管家"),
        "icon": config.get(
            "icon", "https://cdn-icons-png.flaticon.com/512/9334/9334588.png"
        ),
    }
    if config.get("sound"):
        payload["sound"] = config["sound"]
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.post(url, json=payload)
            if resp.status_code == 200:
                return True, "Bark 推送成功"
            return False, f"Bark 返回错误: HTTP {resp.status_code}"
    except Exception as e:
        return False, f"Bark 网络异常: {type(e).__name__}"


def send_qywx_am(
    config: dict[str, Any], title: str, content: str
) -> tuple[bool, str]:
    raw_am = str(config.get("am", "")).strip()
    if raw_am and "," in raw_am:
        parts = [p.strip() for p in raw_am.split(",")]
        corpid = parts[0] if len(parts) > 0 else ""
        corpsecret = parts[1] if len(parts) > 1 else ""
        touser = parts[2] if len(parts) > 2 and parts[2] else "@all"
        agentid = parts[3] if len(parts) > 3 else ""
    else:
        corpid = str(config.get("corpid", "")).strip()
        corpsecret = str(config.get("corpsecret", "")).strip()
        touser = str(config.get("touser", "@all")).strip() or "@all"
        agentid = str(config.get("agentid", "")).strip()

    if not corpid or not corpsecret or not agentid:
        return False, "企业微信应用消息需要配置 corpid, corpsecret, agentid"

    token_url = f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={corpid}&corpsecret={corpsecret}"
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            t_resp = client.get(token_url)
            t_json = t_resp.json()
            if t_json.get("errcode") != 0:
                return False, f"企业微信获取 Token 失败: 错误码 {t_json.get('errcode')}"
            access_token = t_json["access_token"]

            send_url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={access_token}"
            html_content = content.replace(chr(10), "<br>")
            payload = {
                "touser": touser,
                "msgtype": "textcard",
                "agentid": int(agentid),
                "textcard": {
                    "title": title,
                    "description": html_content,
                    "url": "https://work.weixin.qq.com",
                    "btntxt": "详情",
                },
            }
            resp = client.post(send_url, json=payload)
            res_json = resp.json()
            if res_json.get("errcode") == 0:
                return True, "企业微信应用消息推送成功"
            return False, f"企业微信应用错误: 错误码 {res_json.get('errcode')}"
    except Exception as e:
        return False, f"企业微信应用异常: {type(e).__name__}"


def send_ntfy(config: dict[str, Any], title: str, content: str) -> tuple[bool, str]:
    topic = config.get("topic", "").strip()
    if not topic:
        return False, "ntfy Topic 不能为空"
    server = (str(config.get("server") or "")).strip().rstrip("/") or "https://ntfy.sh"
    url = f"{server}/{topic}"
    headers = {
        "Title": title.encode("utf-8").decode("latin-1", errors="replace"),
        "Tags": "credit_card,moneybag",
    }
    if config.get("token"):
        headers["Authorization"] = f"Bearer {config['token'].strip()}"
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.post(url, headers=headers, content=content.encode("utf-8"))
            if resp.status_code == 200:
                return True, "ntfy 推送成功"
            return False, f"ntfy 返回错误: HTTP {resp.status_code}"
    except Exception as e:
        return False, f"ntfy 网络异常: {type(e).__name__}"


def send_serverchan(
    config: dict[str, Any], title: str, content: str
) -> tuple[bool, str]:
    key = config.get("key", "").strip()
    if not key:
        return False, "Server酱 Key 不能为空"
    url = f"https://sctapi.ftqq.com/{key}.send"
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.post(url, data={"title": title, "desp": content})
            res_json = resp.json()
            if res_json.get("code") == 0:
                return True, "Server酱 推送成功"
            return False, f"Server酱 返回错误: 错误码 {res_json.get('code')}"
    except Exception as e:
        return False, f"Server酱 网络异常: {type(e).__name__}"


def send_pushplus(
    config: dict[str, Any], title: str, content: str
) -> tuple[bool, str]:
    token = config.get("token", "").strip()
    if not token:
        return False, "PushPlus Token 不能为空"
    url = "https://www.pushplus.plus/send"
    html_content = content.replace(chr(10), "<br>")
    payload = {
        "token": token,
        "title": title,
        "content": html_content,
        "template": "html",
    }
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.post(url, json=payload)
            res_json = resp.json()
            if res_json.get("code") == 200:
                return True, "PushPlus 推送成功"
            return False, f"PushPlus 返回错误: 错误码 {res_json.get('code')}"
    except Exception as e:
        return False, f"PushPlus 网络异常: {type(e).__name__}"


def send_pushdeer(
    config: dict[str, Any], title: str, content: str
) -> tuple[bool, str]:
    key = str(config.get("key", "")).strip()
    if not key:
        return False, "PushDeer Key 不能为空"
    server = (str(config.get("server") or "")).strip().rstrip("/") or "https://api2.pushdeer.com"
    url = f"{server}/message/push"
    payload = {
        "pushkey": key,
        "text": title,
        "desp": content,
        "type": "markdown",
    }
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.post(url, json=payload)
            if 200 <= resp.status_code < 300:
                return True, "PushDeer 推送已发送"
            return False, f"PushDeer 返回错误: HTTP {resp.status_code}"
    except Exception as e:
        return False, f"PushDeer 网络异常: {type(e).__name__}"


def send_qywx(config: dict[str, Any], title: str, content: str) -> tuple[bool, str]:
    key = config.get("key", "").strip()
    if not key:
        return False, "企业微信机器人 Key 不能为空"
    url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={key}"
    msg = f"{title}\n\n{content}"
    payload = {"msgtype": "text", "text": {"content": msg}}
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.post(url, json=payload)
            res_json = resp.json()
            if res_json.get("errcode") == 0:
                return True, "企业微信推送成功"
            return False, f"企业微信错误: 错误码 {res_json.get('errcode')}"
    except Exception as e:
        return False, f"企业微信网络异常: {type(e).__name__}"


def send_dingtalk(
    config: dict[str, Any], title: str, content: str
) -> tuple[bool, str]:
    token = config.get("token", "").strip()
    if not token:
        return False, "钉钉机器人 Access Token 不能为空"
    url = f"https://oapi.dingtalk.com/robot/send?access_token={token}"
    secret = config.get("secret", "").strip()
    if secret:
        timestamp = str(round(time.time() * 1000))
        secret_enc = secret.encode("utf-8")
        string_to_sign = f"{timestamp}\n{secret}"
        string_to_sign_enc = string_to_sign.encode("utf-8")
        hmac_code = hmac.new(
            secret_enc, string_to_sign_enc, digestmod=hashlib.sha256
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        url += f"&timestamp={timestamp}&sign={sign}"
    msg = f"{title}\n\n{content}"
    payload = {"msgtype": "text", "text": {"content": msg}}
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.post(url, json=payload)
            res_json = resp.json()
            if res_json.get("errcode") == 0:
                return True, "钉钉推送成功"
            return False, f"钉钉错误: 错误码 {res_json.get('errcode')}"
    except Exception as e:
        return False, f"钉钉网络异常: {type(e).__name__}"


def send_feishu(config: dict[str, Any], title: str, content: str) -> tuple[bool, str]:
    key = config.get("key", "").strip()
    if not key:
        return False, "飞书 Webhook Key 不能为空"
    url = (
        key
        if key.startswith("http")
        else f"https://open.feishu.cn/open-apis/bot/v2/hook/{key}"
    )
    msg = f"{title}\n\n{content}"
    payload = {
        "msg_type": "text",
        "content": {"text": msg},
    }
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.post(url, json=payload)
            res_json = resp.json()
            if res_json.get("code") == 0 or res_json.get("StatusCode") == 0:
                return True, "飞书推送成功"
            return False, f"飞书错误: 错误码 {res_json.get('code', res_json.get('StatusCode'))}"
    except Exception as e:
        return False, f"飞书网络异常: {type(e).__name__}"


def send_telegram(
    config: dict[str, Any], title: str, content: str
) -> tuple[bool, str]:
    token = config.get("bot_token", "").strip()
    chat_id = config.get("chat_id", "").strip()
    if not token or not chat_id:
        return False, "Telegram Bot Token 和 Chat ID 不能为空"
    api_url = (str(config.get("api_url") or "")).strip().rstrip("/") or "https://api.telegram.org"
    url = f"{api_url}/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": f"*{title}*\n\n{content}",
        "parse_mode": "Markdown",
    }
    proxy_url = str(config.get("proxy_url", "")).strip()
    client_options: dict[str, Any] = {"timeout": TIMEOUT}
    if proxy_url:
        client_options["proxy"] = proxy_url
    try:
        with httpx.Client(**client_options) as client:
            resp = client.post(url, json=payload)
            res_json = resp.json()
            if res_json.get("ok"):
                return True, "Telegram 推送成功"
            return False, f"Telegram 错误: HTTP {resp.status_code}"
    except Exception as e:
        return False, f"Telegram 网络异常: {type(e).__name__}"


def send_gotify(
    config: dict[str, Any], title: str, content: str
) -> tuple[bool, str]:
    url = str(config.get("url", "")).rstrip("/")
    token = str(config.get("token", "")).strip()
    if not url or not token:
        return False, "Gotify URL 与 Token 不能为空"
    post_url = f"{url}/message?token={token}"
    payload = {
        "title": title,
        "message": content,
        "priority": int(config.get("priority", 5)),
    }
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.post(post_url, json=payload)
            if 200 <= resp.status_code < 300:
                return True, "Gotify 推送已发送"
            return False, f"Gotify 返回错误: HTTP {resp.status_code}"
    except Exception as e:
        return False, f"Gotify 网络异常: {type(e).__name__}"


def send_webhook(config: dict[str, Any], title: str, content: str) -> tuple[bool, str]:
    url = config.get("url", "").strip()
    if not url:
        return False, "Webhook URL 不能为空"
    payload = {
        "title": title,
        "content": content,
        "timestamp": int(time.time()),
    }
    headers = config.get("headers") or {}
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.post(url, json=payload, headers=headers)
            if 200 <= resp.status_code < 300:
                return True, f"Webhook 推送成功 (HTTP {resp.status_code})"
            return False, f"Webhook 返回错误: HTTP {resp.status_code}"
    except Exception as e:
        return False, f"Webhook 网络异常: {type(e).__name__}"


PROVIDERS = {
    "qywx_am": {"name": "企业微信应用消息 (微信直达)", "func": send_qywx_am},
    "bark": {"name": "Bark (iOS)", "func": send_bark},
    "pushplus": {"name": "PushPlus (微信公众号)", "func": send_pushplus},
    "pushdeer": {"name": "PushDeer", "func": send_pushdeer},
    "ntfy": {"name": "ntfy", "func": send_ntfy},
    "feishu": {"name": "飞书机器人", "func": send_feishu},
    "dingtalk": {"name": "钉钉机器人", "func": send_dingtalk},
    "qywx": {"name": "企业微信机器人", "func": send_qywx},
    "serverchan": {"name": "Server酱", "func": send_serverchan},
    "telegram": {"name": "Telegram Bot", "func": send_telegram},
    "gotify": {"name": "Gotify", "func": send_gotify},
    "webhook": {"name": "自定义 Webhook", "func": send_webhook},
}


def dispatch_notification(
    provider: str, config: dict[str, Any], title: str, content: str
) -> tuple[bool, str]:
    prov = PROVIDERS.get(provider.lower())
    if not prov:
        return False, f"不支持的推送渠道: {provider}"
    success, message = prov["func"](config, title, content)
    return success, redact_sensitive(message)
