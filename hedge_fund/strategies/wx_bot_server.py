"""
企业微信【自建应用】回调服务（接收持仓截图 → 分析 → 回复）
=====================================================================
为什么需要自建应用（而非群机器人）：
  群机器人只能【发】消息，不能【收】。要接收用户发来的持仓截图，必须用
  企业微信「自建应用 + 接收消息」回调。本服务用 Python 标准库 http.server 实现，
  仅额外依赖 pycryptodome（AES 解密回调）。

部署步骤：
  1. 企微管理后台 → 应用管理 → 自建 → 创建应用，拿到 corp_id / agent_id / secret。
  2. 应用 → 接收消息 → 设置 API 接收：
       URL:  https://<你的公网域名>/wx/callback
       Token: 与下方 WX_TOKEN 一致
       EncodingAESKey: 与下方 WX_AES_KEY 一致（43 位）
  3. 公网暴露：本服务监听 WX_BOT_PORT（默认 8080）。可用 云服务器 / 内网穿透(ngrok) 等。
  4. 配置 .env：WX_CORP_ID / WX_AGENT_ID / WX_CORP_SECRET / WX_TOKEN / WX_AES_KEY
  5. 启动： python wx_bot_server.py

交互：
  - 用户发【图片】→ 识别持仓 → 更新 my_holdings.json → 推送每日详细总结
  - 用户发【文字 复盘/总结/每日】→ 用已存持仓做每日总结
  - 用户发【文字 反馈: …】→ 沉淀一条经验到 Skill 记忆（自动进化）

注意：分析耗时 > 5 秒，故先被动回复「收到，分析中…」，再用应用消息异步推送结果。
"""

import base64
import hashlib
import os
import struct
import sys
import threading
import time
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from typing import Optional

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 📝 自动加载 .env（cron/手动运行都需；否则企微凭证/WECHAT_WEBHOOK 等为空）
try:
    from _env import load_env
except ImportError:
    from hedge_fund.strategies._env import load_env
load_env()

# 视觉/分析模块
import holding_analyzer as ha

WX_CORP_ID = os.environ.get("WX_CORP_ID", "").strip()
WX_AGENT_ID = os.environ.get("WX_AGENT_ID", "").strip()
WX_CORP_SECRET = os.environ.get("WX_CORP_SECRET", "").strip()
WX_TOKEN = os.environ.get("WX_TOKEN", "").strip()
WX_AES_KEY = os.environ.get("WX_AES_KEY", "").strip()
WX_BOT_HOST = os.environ.get("WX_BOT_HOST", "0.0.0.0")
WX_BOT_PORT = int(os.environ.get("WX_BOT_PORT", "8080"))

_QYAPI = "https://qyapi.weixin.qq.com/cgi-bin"


# ==========================================================
# 企业微信消息加解密（WXBizMsgCrypt，AES-256-CBC）
# ==========================================================
class WXBizMsgCrypt:
    def __init__(self, token: str, encoding_aes_key: str, receive_id: str):
        self.token = token
        self.key = base64.b64decode(encoding_aes_key + "=")
        self.iv = self.key[:16]
        self.receive_id = receive_id

    @staticmethod
    def _pkcs7_pad(text: bytes, block: int = 32) -> bytes:
        pad = block - (len(text) % block)
        if pad == 0:
            pad = block
        return text + bytes([pad]) * pad

    @staticmethod
    def _pkcs7_unpad(text: bytes) -> bytes:
        pad = text[-1]
        if pad < 1 or pad > 32:
            return text
        return text[:-pad]

    def _signature(self, *parts: str) -> str:
        sha = hashlib.sha1()
        sha.update("".join(sorted(parts)).encode("utf-8"))
        return sha.hexdigest()

    def _decrypt(self, encrypt_b64: str) -> bytes:
        from Crypto.Cipher import AES
        cipher = AES.new(self.key, AES.MODE_CBC, self.iv)
        plain = cipher.decrypt(base64.b64decode(encrypt_b64))
        plain = self._pkcs7_unpad(plain)
        msg_len = struct.unpack(">I", plain[16:20])[0]
        msg = plain[20:20 + msg_len]
        from_id = plain[20 + msg_len:]
        if from_id.decode("utf-8", "ignore") != self.receive_id:
            raise ValueError("receive_id 校验失败（corp_id 不匹配）")
        return msg

    def _encrypt(self, reply: str, nonce: str, timestamp: str) -> str:
        from Crypto.Cipher import AES
        body = (os.urandom(16)
                + struct.pack(">I", len(reply.encode("utf-8")))
                + reply.encode("utf-8")
                + self.receive_id.encode("utf-8"))
        cipher = AES.new(self.key, AES.MODE_CBC, self.iv)
        enc = base64.b64encode(cipher.encrypt(self._pkcs7_pad(body))).decode("ascii")
        return enc

    def verify_url(self, msg_signature: str, timestamp: str, nonce: str, echostr: str) -> Optional[str]:
        if self._signature(self.token, timestamp, nonce, echostr) != msg_signature:
            return None
        try:
            return self._decrypt(echostr).decode("utf-8")
        except Exception:
            return None

    def decrypt_msg(self, msg_signature: str, timestamp: str, nonce: str, encrypt_b64: str) -> str:
        if self._signature(self.token, timestamp, nonce, encrypt_b64) != msg_signature:
            raise ValueError("消息签名校验失败")
        return self._decrypt(encrypt_b64).decode("utf-8")

    def encrypt_reply(self, reply: str, nonce: str, timestamp: str) -> str:
        enc = self._encrypt(reply, nonce, timestamp)
        return f"<xml><Encrypt><![CDATA[{enc}]]></Encrypt></xml>"


# ==========================================================
# 企业微信 access_token + 应用消息发送
# ==========================================================
_token_cache = {"token": "", "expire": 0}


def get_access_token() -> str:
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expire"] - 60:
        return _token_cache["token"]
    r = requests.get(f"{_QYAPI}/gettoken", params={
        "corpid": WX_CORP_ID, "corpsecret": WX_CORP_SECRET,
    }, timeout=10)
    data = r.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"获取企微 access_token 失败: {data}")
    _token_cache["token"] = data["access_token"]
    _token_cache["expire"] = now + data.get("expires_in", 7200)
    return _token_cache["token"]


def send_app_message(user_id: str, content: str):
    """通过自建应用给指定用户发送文本消息（超长自动分段，避免 2048 字节上限）。"""
    token = get_access_token()
    # 按 ~1500 字符分段
    chunks = ha.split_wechat_message(content, 1500) if ha.split_wechat_message else [content]
    for i, chunk in enumerate(chunks, 1):
        payload = {
            "touser": user_id,
            "msgtype": "text",
            "agentid": int(WX_AGENT_ID) if WX_AGENT_ID.isdigit() else WX_AGENT_ID,
            "text": {"content": f"（{i}/{len(chunks)}）\n{chunk}"},
        }
        try:
            r = requests.post(f"{_QYAPI}/message/send",
                              params={"access_token": token}, json=payload, timeout=10)
            if r.json().get("errcode") != 0:
                print(f"⚠️ 应用消息发送失败: {r.text}")
        except Exception as e:
            print(f"⚠️ 应用消息发送异常: {e}")
        time.sleep(0.5)


def download_media(media_id: str) -> bytes:
    token = get_access_token()
    r = requests.get(f"{_QYAPI}/media/get",
                     params={"access_token": token, "media_id": media_id}, timeout=15)
    if r.status_code == 200 and r.content:
        return r.content
    raise RuntimeError(f"下载媒体文件失败: {r.status_code}")


# ==========================================================
# 后台处理（耗时操作，避免阻塞 5 秒被动回复）
# ==========================================================
def _handle_image(user_id: str, media_id: str):
    try:
        img = download_media(media_id)
        mime = "image/jpeg"
        result = ha.run_from_image_bytes(img, mime)
        send_app_message(user_id, result)
    except Exception as e:
        send_app_message(user_id, f"⚠️ 持仓分析失败：{e}")


def _handle_text(user_id: str, text: str):
    try:
        if any(k in text for k in ("复盘", "总结", "每日", "持仓")):
            result = ha.run_daily()
            send_app_message(user_id, result)
        elif text.startswith("反馈") or text.startswith("教训") or text.startswith("经验"):
            lesson = text.split(":", 1)[-1].split("：", 1)[-1].strip()
            if lesson:
                ha.ExperienceMemory().add_lesson(lesson, tag="用户反馈")
                send_app_message(user_id, f"🧠 已记录经验：{lesson[:50]}…")
            else:
                send_app_message(user_id, "请输入『反馈: 你的经验文字』来沉淀一条经验。")
        else:
            send_app_message(user_id, "可发送：①持仓截图（自动识别+分析）②文字『复盘』（每日总结）③『反馈: …』（沉淀经验）。")
    except Exception as e:
        send_app_message(user_id, f"⚠️ 处理失败：{e}")


# ==========================================================
# HTTP 回调处理
# ==========================================================
class CallbackHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(length) if length else b""

    def _respond(self, code: int, body: str = "", ctype: str = "text/plain"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def do_GET(self):
        # URL 校验
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        msg_sig = qs.get("msg_signature", [""])[0]
        timestamp = qs.get("timestamp", [""])[0]
        nonce = qs.get("nonce", [""])[0]
        echostr = qs.get("echostr", [""])[0]
        crypt = WXBizMsgCrypt(WX_TOKEN, WX_AES_KEY, WX_CORP_ID)
        plain = crypt.verify_url(msg_sig, timestamp, nonce, echostr)
        if plain is None:
            self._respond(403, "verify failed")
        else:
            self._respond(200, plain)

    def do_POST(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        msg_sig = qs.get("msg_signature", [""])[0]
        timestamp = qs.get("timestamp", [""])[0]
        nonce = qs.get("nonce", [""])[0]
        body = self._read_body().decode("utf-8", "ignore")

        crypt = WXBizMsgCrypt(WX_TOKEN, WX_AES_KEY, WX_CORP_ID)
        try:
            enc = ET.fromstring(body).findtext("Encrypt") or ""
            msg_xml = crypt.decrypt_msg(msg_sig, timestamp, nonce, enc)
        except Exception as e:
            self._respond(403, f"decrypt failed: {e}")
            return

        try:
            root = ET.fromstring(msg_xml)
            msg_type = root.findtext("MsgType", "")
            user_id = root.findtext("FromUserName", "")
            if msg_type == "image":
                media_id = root.findtext("MediaId", "")
                # 先被动回复（5 秒内）
                reply = crypt.encrypt_reply("收到，正在识别您的持仓截图并分析，请稍候…",
                                           nonce, timestamp)
                self._respond(200, reply, "application/xml")
                threading.Thread(target=_handle_image, args=(user_id, media_id), daemon=True).start()
                return
            elif msg_type == "text":
                content = root.findtext("Content", "")
                self._respond(200, crypt.encrypt_reply("收到，处理中…", nonce, timestamp), "application/xml")
                threading.Thread(target=_handle_text, args=(user_id, content), daemon=True).start()
                return
        except Exception as e:
            self._respond(200, crypt.encrypt_reply("消息解析异常", nonce, timestamp), "application/xml")
            print(f"⚠️ 消息处理异常: {e}")
            return

        self._respond(200, crypt.encrypt_reply("ok", nonce, timestamp), "application/xml")

    def log_message(self, fmt, *args):
        sys.stderr.write("[wx_bot] " + (fmt % args) + "\n")


def main():
    missing = [n for n, v in [
        ("WX_CORP_ID", WX_CORP_ID), ("WX_AGENT_ID", WX_AGENT_ID),
        ("WX_CORP_SECRET", WX_CORP_SECRET), ("WX_TOKEN", WX_TOKEN),
        ("WX_AES_KEY", WX_AES_KEY),
    ] if not v]
    if missing:
        print(f"⚠️ 缺少企业微信自建应用配置：{', '.join(missing)}，请在 .env 中填写后启动。")
        sys.exit(1)

    server = ThreadingHTTPServer((WX_BOT_HOST, WX_BOT_PORT), CallbackHandler)
    print(f"🤖 企业微信持仓分析机器人已启动： http://{WX_BOT_HOST}:{WX_BOT_PORT}/wx/callback")
    print("   请将该地址（需公网可访问）配置到企微应用的『接收消息』回调。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 服务已停止。")


if __name__ == "__main__":
    main()
