"""
屏幕日记 —— 视觉分析

把一天的截图一张张喂给本地视觉模型，各换一句话描述。

关键设计：
  * 只读图，不实时。白天采集时一次都不调用模型，所以白天完全不占 GPU。
  * 图缩放成 896 宽再送（见 screenshots.py 里的说明），这是快 15 倍的关键。
  * 分析完就删图。图是临时的，日记才是资产。
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from config import OLLAMA_API, VISION_MODEL

VISION_PROMPT = (
    "这是电脑屏幕的截图。用一句中文说清楚：这个人在用什么软件、可能在做什么事。"
    "要求：不超过 30 个字，直接描述，不要客套话，不要说“这张图显示”这种开头。"
)


class OllamaError(RuntimeError):
    pass


def _post(path: str, payload: dict, timeout: float):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_API}{path}", data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        raise OllamaError(f"HTTP {e.code}: {body}") from None
    except urllib.error.URLError as e:
        raise OllamaError(f"连不上 Ollama：{e.reason}") from None


def _get(path: str, timeout: float = 15):
    try:
        with urllib.request.urlopen(f"{OLLAMA_API}{path}", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def server_alive() -> bool:
    return _get("/api/version") is not None


def model_capabilities(model: str) -> list[str]:
    try:
        with _post("/api/show", {"name": model}, 30) as r:
            obj = json.loads(r.read().decode("utf-8"))
        return obj.get("capabilities", []) or []
    except Exception:
        return []


def has_vision(model: str = VISION_MODEL) -> bool:
    return "vision" in model_capabilities(model)


def describe_image(path: Path, model: str = VISION_MODEL,
                   timeout: float = 300) -> tuple[str, float]:
    """
    让视觉模型描述一张图。返回 (描述, 耗时秒)。
    """
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": VISION_PROMPT,
            "images": [b64],
        }],
        "stream": False,
        "think": False,
        "keep_alive": "10m",       # 批量分析期间别来回卸载
        "options": {"num_ctx": 4096, "num_predict": 90, "temperature": 0.4},
    }
    t0 = time.time()
    with _post("/api/chat", payload, timeout) as r:
        obj = json.loads(r.read().decode("utf-8"))
    text = (obj.get("message", {}).get("content") or "").strip()
    text = text.replace("\n", " ").strip()
    return text, time.time() - t0


def unload(model: str = VISION_MODEL):
    """把模型从显存踢出去，还显存给用户。"""
    try:
        with _post("/api/generate", {"model": model, "keep_alive": 0}, 20) as r:
            r.read()
        return True
    except Exception:
        return False


def running_models() -> list[dict]:
    return (_get("/api/ps") or {}).get("models", []) or []
