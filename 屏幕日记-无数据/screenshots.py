"""
屏幕日记 —— 截图

只在「窗口切换并稳定下来」的时候才拍，所以一天几百张而不是 1440 张。

两个实测得出的关键点：
  1. 必须缩放到 896 宽。视觉模型内部只吃固定数量的 patch，图再大不会更准，
     只会更慢 —— 实测 2048 宽要 17 秒且看错，896 宽只要 1.1 秒且看对。
  2. 存 JPEG 不存 PNG/BMP。实测同屏：JPEG 291KB/26ms，PNG 655KB/59ms，
     BMP 9MB（一天能攒 12.7GB，千万别用）。
"""

from __future__ import annotations

import time
from pathlib import Path

from PIL import Image, ImageGrab

from config import SHOT_MAX_WIDTH, SHOT_QUALITY


class Shooter:
    def __init__(self, max_width: int = SHOT_MAX_WIDTH, quality: int = SHOT_QUALITY,
                 logger=None):
        self.max_width = max_width
        self.quality = quality
        self.log = logger or (lambda *a, **k: None)
        self.count = 0
        self.last_error = ""

    def grab(self, out_dir: Path, tag: str = "") -> tuple[Path | None, dict]:
        """
        拍一张，缩放后存 JPEG。返回 (路径, 信息)。
        失败不抛异常 —— 截屏失败不该让整个记录中断。
        """
        info = {"ok": False, "ms": 0, "kb": 0, "w": 0, "h": 0, "err": ""}
        t0 = time.time()
        try:
            img = ImageGrab.grab()
            orig_w, orig_h = img.size

            if orig_w > self.max_width:
                new_h = max(1, int(orig_h * self.max_width / orig_w))
                img = img.resize((self.max_width, new_h), Image.LANCZOS)

            out_dir.mkdir(parents=True, exist_ok=True)
            name = time.strftime("%H%M%S")
            path = out_dir / f"{name}{('_' + tag) if tag else ''}.jpg"
            n = 1
            while path.exists():
                path = out_dir / f"{name}{('_' + tag) if tag else ''}_{n}.jpg"
                n += 1

            img.convert("RGB").save(path, "JPEG", quality=self.quality,
                                    optimize=True)
            el = (time.time() - t0) * 1000

            info.update({
                "ok": True, "ms": round(el), "kb": round(path.stat().st_size / 1024),
                "w": img.size[0], "h": img.size[1],
                "orig": f"{orig_w}x{orig_h}",
            })
            self.count += 1
            return path, info
        except Exception as e:
            info["err"] = str(e)
            self.last_error = str(e)
            self.log(f"[截图] 失败: {e}")
            return None, info


def day_shot_usage(day_dir: Path) -> tuple[int, float]:
    """返回 (张数, 总 MB)。"""
    if not day_dir.exists():
        return 0, 0.0
    files = list(day_dir.glob("*.jpg"))
    total = sum(f.stat().st_size for f in files)
    return len(files), total / 1024 / 1024


def cleanup_old_shots(shot_root: Path, keep_days: int | None = None,
                      logger=None):
    """
    删掉超过保留期的截图目录。

    keep_days 不给就取配置里的值。别在这里写死字面量 ——
    这里曾经写死 1，而配置里是 3，两处不一致。运行时虽然总是显式传参
    （journal.py 会传），但直接调这个函数的人会踩到，所以改成从配置读。
    """
    log = logger or (lambda *a, **k: None)
    if keep_days is None:
        try:
            from config import KEEP_SHOTS_DAYS
            keep_days = KEEP_SHOTS_DAYS
        except Exception:
            keep_days = 3
    if not shot_root.exists():
        return 0
    import datetime as _dt
    cutoff = _dt.date.today() - _dt.timedelta(days=keep_days)
    removed = 0
    for d in shot_root.iterdir():
        if not d.is_dir():
            continue
        try:
            d_day = _dt.datetime.strptime(d.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if d_day < cutoff:
            try:
                for f in d.glob("*.jpg"):
                    f.unlink()
                d.rmdir()
                removed += 1
            except Exception:
                pass
    if removed:
        log(f"[清理] 删除了 {removed} 个过期截图目录")
    return removed
