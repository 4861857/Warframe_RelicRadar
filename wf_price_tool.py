# -*- coding: utf-8 -*-
"""
虚空裂缝报酬 自动估价
================================
按一个热键 -> 截取屏幕 -> OCR 识别道具名 -> 查 warframe.market 价格 -> 弹窗显示
同时标注每个道具的「入库 / 出库」状态（入库=遗物已入库，通常价格更高）。

用法:
    python wf_price_tool.py              正常启动，按热键触发
    python wf_price_tool.py --image X    识别一张图片后直接退出（测试用）
    python wf_price_tool.py --refresh    重新下载道具名单 + 遗物掉落缓存

依赖: 见 requirements.txt
"""

import json
import os
import queue
import sys
import threading
import time
import urllib.request
import urllib.error

# ===================== 配置 =====================
HOTKEY = "f9"                        # 触发热键（单个键名，如 f9 / f10 / ctrl+shift+p）
PROXY = None                         # 默认直连；若需代理填 "http://127.0.0.1:7890"
MONITOR = 1                          # 截图的显示器编号（1 = 主屏）
MATCH_CUTOFF = 85                    # 道具名匹配阈值（0-100，越高越严格）
PRICE_TIMEOUT = 8                    # 查价请求超时（秒）
CAPTURE_TIMEOUT = 30                 # 单次识别看门狗超时（秒），卡住时强制解锁热键
DIRECT_TIMEOUT = 5                   # 直连探测超时（秒），失败后回退本地代理
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "item_names.json")
RELIC_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "relic_drops.json")
PRICE_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "price_cache.json")
API_BASE = "https://api.warframe.market/v2"
# 遗物掉落表（item -> 来源遗物）来源：WFCD/warframe-drop-data
DROP_DATA_URL = ("https://raw.githubusercontent.com/WFCD/warframe-drop-data/"
                 "master/data/relics.json")
# =================================================

HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- 网络层
def _registry_proxy():
    """读取 Windows 系统代理（即使系统代理未启用，也作为兜底）。"""
    try:
        import winreg
        k = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        try:
            v, _ = winreg.QueryValueEx(k, "ProxyServer")
        finally:
            winreg.CloseKey(k)
        if isinstance(v, str) and v and "=" not in v and v.startswith("http"):
            return v
    except Exception:
        pass
    return None


def _make_opener(proxy):
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    # 直连：urllib 会读取环境变量 HTTP(S)_PROXY 与系统代理
    return urllib.request.build_opener()


# 记住第一次成功的代理，之后复用，避免每次都重新探测
_UNSET = object()
_PREFERRED_PROXY = _UNSET
MAX_RETRIES = 3                      # 瞬时网络错误（连接重置/超时）的重试次数


def urlopen_json(url, timeout=PRICE_TIMEOUT):
    """请求 JSON。默认直连；失败后自动回退到系统代理（并记住）。"""
    global _PREFERRED_PROXY
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/json",
        "Platform": "pc",
        "Language": "zh-hans",
    }

    if _PREFERRED_PROXY is not _UNSET:
        candidates = [None if _PREFERRED_PROXY is None else _PREFERRED_PROXY]
    else:
        if PROXY is not None:
            candidates = [PROXY]
        else:
            candidates = [None]                # 直连（读环境变量/系统代理）
            rp = _registry_proxy()
            if rp:
                candidates.append(rp)          # 直连失败后兜底用系统代理

    last = None
    for p in candidates:
        opener = _make_opener(p)
        t = DIRECT_TIMEOUT if (p is None and PROXY is None and len(candidates) > 1) else timeout
        for attempt in range(MAX_RETRIES):
            req = urllib.request.Request(url, headers=headers)
            try:
                with opener.open(req, timeout=t) as r:
                    data = json.loads(r.read().decode("utf-8"))
                _PREFERRED_PROXY = p
                return data
            except urllib.error.HTTPError as e:
                # 5xx / 限流才值得重试；其余 HTTP 错误放弃该通道
                if e.code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES - 1:
                    last = e
                    time.sleep(0.6 * (attempt + 1))
                    continue
                last = e
                break
            except Exception as e:
                # 连接重置(10054)、超时等瞬时错误：换新连接重试
                last = e
                if attempt < MAX_RETRIES - 1:
                    time.sleep(0.6 * (attempt + 1))
                    continue
    raise last


# ---------------------------------------------------------------- 道具名单
class ItemRegistry:
    def __init__(self):
        self.items = []           # [{slug, zh, en, vaulted}]
        self.norm_list = []
        self._norm_map = {}
        self.relic_vaulted = {}   # relic slug -> bool
        self.item2relic = {}      # item 英文名 -> [遗物全名]

    @staticmethod
    def norm(s):
        return s.replace(" ", "").replace("　", "").lower()

    # ---- 加载 / 下载 ----
    def load(self, force_refresh=False):
        if not force_refresh and os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                self.items = json.load(f)
        else:
            self.items = self._download_items()

        if not force_refresh and os.path.exists(RELIC_FILE):
            with open(RELIC_FILE, "r", encoding="utf-8") as f:
                self.item2relic = json.load(f)
        else:
            self.item2relic = self._download_relic_drops()

        self.norm_list = [self.norm(it["zh"]) for it in self.items]
        self._norm_map = {n: i for i, n in enumerate(self.norm_list)}
        self.relic_vaulted = {
            it["slug"]: it["vaulted"]
            for it in self.items
            if it.get("vaulted") is not None
        }

    def _download_items(self):
        print("正在下载道具名单...", flush=True)
        data = urlopen_json(f"{API_BASE}/items")["data"]
        items = []
        for it in data:
            zh = it.get("i18n", {}).get("zh-hans", {}).get("name", "")
            en = it.get("i18n", {}).get("en", {}).get("name", "")
            if zh:
                items.append({
                    "slug": it["slug"], "zh": zh, "en": en,
                    "vaulted": it.get("vaulted"),   # 只有遗物会有值
                })
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False)
        print(f"道具名单已缓存: {len(items)} 项")
        return items

    def _download_relic_drops(self):
        print("正在下载遗物掉落表...", flush=True)
        data = urlopen_json(DROP_DATA_URL)
        relics = data.get("relics", []) if isinstance(data, dict) else data
        uniq = {}
        for r in relics:
            if "relicName" not in r:          # 跳过 Requiem 遗物
                continue
            key = (r.get("tier", ""), r["relicName"])
            uniq.setdefault(key, set())
            for rw in r.get("rewards", []):
                uniq[key].add(rw.get("itemName", ""))
        item2relic = {}
        for (tier, name), items in uniq.items():
            full = f"{tier} {name} Relic"
            for it in items:
                if it:
                    item2relic.setdefault(it, set()).add(full)
        item2relic = {k: sorted(v) for k, v in item2relic.items()}
        with open(RELIC_FILE, "w", encoding="utf-8") as f:
            json.dump(item2relic, f, ensure_ascii=False)
        print(f"遗物掉落表已缓存: {len(item2relic)} 个道具")
        return item2relic

    # ---- 匹配 ----
    def match(self, text, cutoff=MATCH_CUTOFF):
        from rapidfuzz import fuzz
        tn = self.norm(text)
        if not tn:
            return None
        idx = self._norm_map.get(tn)
        if idx is not None:
            return self.items[idx]
        best, best_score = None, 0.0
        for i, name in enumerate(self.norm_list):
            score = fuzz.ratio(tn, name)
            if score > best_score:
                best_score, best = score, self.items[i]
        return best if best_score >= cutoff else None

    # ---- 入库/出库判定 ----
    def vaulted_status(self, item):
        """返回 True=入库, False=出库, None=未知。"""
        from rapidfuzz import fuzz
        en = item.get("en", "")
        relics = self.item2relic.get(en)
        if relics is None and en:            # 名字可能略有差异，做一次模糊匹配
            best, best_score = None, 0.0
            for key in self.item2relic:
                sc = fuzz.ratio(self.norm(en), self.norm(key))
                if sc > best_score:
                    best_score, best = sc, key
            if best_score >= 88:
                relics = self.item2relic[best]
        if not relics:
            return None
        statuses = []
        for r in relics:
            slug = r.replace(" ", "_").lower()
            statuses.append(self.relic_vaulted.get(slug))
        known = [s for s in statuses if s is not None]
        if not known:
            return None
        # 只要有一个来源遗物仍可掉落（未入库），道具就算出库
        return not any(s is False for s in known)


# ---------------------------------------------------------------- 价格
def get_item_price(slug):
    data = urlopen_json(f"{API_BASE}/orders/item/{slug}/top", timeout=PRICE_TIMEOUT)
    d = data.get("data", {}) or {}
    sells = d.get("sell") or []
    buys = d.get("buy") or []
    low_sell = min((o.get("platinum") for o in sells if o.get("platinum") is not None),
                   default=None)
    high_buy = max((o.get("platinum") for o in buys if o.get("platinum") is not None),
                   default=None)
    return low_sell, high_buy


class PriceCache:
    """价格缓存：识别后先显示上次缓存价，后台刷新后再更新。"""
    def __init__(self, path):
        self.path = path
        self.data = {}
        self.load()

    def load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
        except Exception:
            self.data = {}

    def get(self, slug):
        return self.data.get(slug)          # {"low":..., "high":..., "ts":...}

    def update(self, prices):
        now = time.time()
        for slug, p in prices.items():
            self.data[slug] = {"low": p["low"], "high": p["high"], "ts": now}
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False)
        except Exception:
            pass


# ---------------------------------------------------------------- 截图 + OCR
def capture_screen():
    import mss
    import numpy as np
    with mss.MSS() as sct:
        mon = sct.monitors[MONITOR]
        shot = sct.grab(mon)
        img = np.asarray(shot)[:, :, :3]   # BGRA -> BGR
    return img


_OCR_ENGINE = None
_OCR_LOCK = threading.Lock()        # OCR 引擎非线程安全，识别线程可能并发，需加锁


def ocr_image(img):
    from rapidocr_onnxruntime import RapidOCR
    global _OCR_ENGINE
    with _OCR_LOCK:
        if _OCR_ENGINE is None:
            _OCR_ENGINE = RapidOCR()
        result, _ = _OCR_ENGINE(img)
    if not result:
        return []
    return [(box, text, float(score)) for box, text, score in result]


# ---------------------------------------------------------------- 预热
def warmup():
    """后台预热：确定可用网络通道 + 预加载 OCR 模型，减少首次按键延迟。"""
    try:
        urlopen_json(f"{API_BASE}/versions", timeout=8)
    except Exception:
        pass
    try:
        global _OCR_ENGINE
        from rapidocr_onnxruntime import RapidOCR
        if _OCR_ENGINE is None:
            _OCR_ENGINE = RapidOCR()
    except Exception:
        pass


# ---------------------------------------------------------------- 主流程
def identify_items(registry, image=None):
    """截图 + OCR + 匹配，返回识别出的道具 [{slug, zh, en}]。"""
    img = image if image is not None else capture_screen()
    lines = ocr_image(img)
    seen, items = set(), []
    for _, text, _ in lines:
        it = registry.match(text)
        if it and it["slug"] not in seen:
            seen.add(it["slug"])
            items.append(it)
    return items


def fetch_fresh_prices(items):
    """并行拉取最新价，返回 {slug: {"low": ..., "high": ...}}。"""
    from concurrent.futures import ThreadPoolExecutor

    def fetch(it):
        try:
            low, high = get_item_price(it["slug"])
        except Exception:
            low, high = None, None          # 单个查价失败不影响其它道具
        return it["slug"], {"low": low, "high": high}

    prices = {}
    if items:
        with ThreadPoolExecutor(max_workers=len(items)) as ex:   # 并行查价
            for slug, p in ex.map(fetch, items):
                prices[slug] = p
    return prices


def build_results(registry, items, prices):
    """items + 价格字典 {slug:{low,high[,ts]}} -> 结果列表。"""
    results = []
    for it in items:
        p = prices.get(it["slug"])
        results.append({
            "zh": it["zh"], "en": it["en"], "slug": it["slug"],
            "low_sell": p["low"] if p else None,
            "high_buy": p["high"] if p else None,
            "cache_ts": p.get("ts") if p else None,   # 缓存价的时间戳（无则 None）
            "vaulted": registry.vaulted_status(it),
        })
    return results


def process_once(registry, image=None):
    """一次性：识别 + 查价（--image 模式用，无缓存）。"""
    items = identify_items(registry, image)
    if not items:
        return []
    return build_results(registry, items, fetch_fresh_prices(items))


# ---------------------------------------------------------------- GUI
def build_ui():
    import tkinter as tk
    import tkinter.font as tkfont

    root = tk.Tk()
    root.title("虚空裂缝报酬估价")
    root.attributes("-topmost", True)
    root.configure(bg="#14161a")
    root.geometry("700x500")          # 启动时的窗口大小
    root.minsize(700, 500)            # 最小尺寸

    f_name = tkfont.Font(family="Microsoft YaHei UI", size=20, weight="bold")
    f_sub = tkfont.Font(family="Microsoft YaHei UI", size=18)
    f_price = tkfont.Font(family="Microsoft YaHei UI", size=20, weight="bold")
    f_badge = tkfont.Font(family="Microsoft YaHei UI", size=20, weight="bold")

    # 顶栏：状态文字 + 右上角图钉（切换是否置顶）
    topbar = tk.Frame(root, bg="#14161a")
    topbar.pack(fill="x", padx=12, pady=(10, 2))

    status = tk.Label(topbar, text="正在初始化...", bg="#14161a", fg="#8b93a1",
                      font=f_sub, anchor="w")
    status.pack(side="left", fill="x", expand=True)

    pin_state = {"on": True}

    def _update_pin():
        if pin_state["on"]:
            pin_btn.config(text="📌", bg="#e0a33a", fg="#14161a",
                           activebackground="#e0a33a", activeforeground="#14161a")
        else:
            pin_btn.config(text="📌", bg="#2c313a", fg="#8b93a1",
                           activebackground="#2c313a", activeforeground="#8b93a1")

    def toggle_pin():
        pin_state["on"] = not pin_state["on"]
        root.attributes("-topmost", pin_state["on"])
        _update_pin()

    pin_btn = tk.Button(topbar, text="📌", command=toggle_pin, relief="flat", bd=0,
                        font=f_sub, cursor="hand2", padx=6)
    pin_btn.pack(side="right")
    _update_pin()

    # 滚动区域：Canvas + 滚动条，条目多时可滚动
    frame = tk.Frame(root, bg="#14161a")
    frame.pack(fill="both", expand=True, padx=8, pady=(0, 10))

    canvas = tk.Canvas(frame, bg="#14161a", highlightthickness=0)
    scrollbar = tk.Scrollbar(frame, orient="vertical", command=canvas.yview)
    inner = tk.Frame(canvas, bg="#14161a")

    inner.bind("<Configure>",
               lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    _win = canvas.create_window((0, 0), window=inner, anchor="nw")

    def _sync_width(_e):
        canvas.itemconfig(_win, width=canvas.winfo_width())

    canvas.bind("<Configure>", _sync_width)

    def _on_mousewheel(event):
        canvas.yview_scroll(int(-event.delta / 120), "units")

    root.bind_all("<MouseWheel>", _on_mousewheel)   # 单窗口应用，滚轮统一滚结果区

    canvas.configure(yscrollcommand=scrollbar.set)
    canvas.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")

    return root, status, inner, (f_name, f_sub, f_price, f_badge)


def _vaulted_label(v):
    if v is True:
        return ("已入库", "#e0a33a")     # 入库，价格通常更高
    if v is False:
        return ("出库中", "#6bc17b")     # 未入库，仍可掉落
    return ("未知", "#5a616b")


def _fmt_age(ts):
    """把时间戳格式化成「刚刚 / N分钟前 / N小时前 / N天前」。"""
    if not ts:
        return None
    sec = max(0, int(time.time() - ts))
    if sec < 60:
        return "刚刚"
    if sec < 3600:
        return f"{sec // 60}分钟前"
    if sec < 86400:
        return f"{sec // 3600}小时前"
    return f"{sec // 86400}天前"


def render_results(root, status, frame, fonts, results, err=None, status_text=None):
    import tkinter as tk
    f_name, f_sub, f_price, f_badge = fonts
    for w in frame.winfo_children():
        w.destroy()
    frame.master.yview_moveto(0.0)   # 每次刷新回滚到顶部

    if err:
        status.config(text=err, fg="#e05c5c")
    elif not results:
        status.config(text=status_text or "未识别到道具，请确认画面停留在报酬界面", fg="#d8b34a")
    else:
        status.config(text=status_text or f"识别完成 · {len(results)} 件 · 按 {HOTKEY.upper()} 重新识别", fg="#6bc17b")

    if not results:
        return

    for r in results:
        card = tk.Frame(frame, bg="#1e2229", bd=0, highlightthickness=1,
                        highlightbackground="#2c313a")
        card.pack(fill="x", pady=3)

        top = tk.Frame(card, bg="#1e2229")
        top.pack(fill="x", padx=10, pady=(7, 2))
        tk.Label(top, text=r["zh"], bg="#1e2229", fg="#ffffff",
                 font=f_name, anchor="w").pack(side="left")
        tk.Label(top, text=r["en"], bg="#1e2229", fg="#7a828d",
                 font=f_sub, anchor="w").pack(side="left", padx=(8, 0))

        badge_text, badge_color = _vaulted_label(r["vaulted"])
        tk.Label(top, text=badge_text, bg="#14161a", fg=badge_color, font=f_badge,
                 padx=6, pady=1).pack(side="right")

        low, high = r["low_sell"], r["high_buy"]
        bot = tk.Frame(card, bg="#1e2229")
        bot.pack(fill="x", padx=10, pady=(0, 7))
        tk.Label(bot, text=f"卖家最低 {low if low is not None else '—'} 白金",
                 bg="#1e2229", fg="#e0a33a" if low is not None else "#5a616b",
                 font=f_price).pack(side="left")
        tk.Label(bot, text=f"买家最高 {high if high is not None else '—'} 白金",
                 bg="#1e2229", fg="#3ab0d8" if high is not None else "#5a616b",
                 font=f_price).pack(side="left", padx=(18, 0))

        age = _fmt_age(r.get("cache_ts"))
        if age:
            tk.Label(bot, text=f"缓存·{age}", bg="#1e2229", fg="#5a616b",
                     font=f_sub).pack(side="right")


# ---------------------------------------------------------------- 入口
def run_gui(registry):
    import tkinter as tk
    root, status, frame, fonts = build_ui()

    # 后台预热：确定网络通道 + 加载 OCR 模型，减少首次按键延迟
    threading.Thread(target=warmup, daemon=True).start()

    price_cache = PriceCache(PRICE_CACHE_FILE)

    q = queue.Queue()
    busy = threading.Event()

    def capture_worker():
        # 重活（截图/OCR/查价）放在独立线程里做，绝不占用 keyboard 的监听线程。
        try:
            items = identify_items(registry)
            if not items:
                q.put(("result", [], "未识别到道具，请确认画面停留在报酬界面"))
                return

            # 1) 先用缓存价立即显示
            cached = {it["slug"]: price_cache.get(it["slug"]) for it in items}
            q.put(("result", build_results(registry, items, cached),
                   f"识别完成 · {len(items)} 件 · 显示缓存价，更新中…"))

            # 2) 后台刷新最新价，刷新显示并写入缓存
            fresh = fetch_fresh_prices(items)
            price_cache.update(fresh)
            q.put(("result", build_results(registry, items, fresh),
                   f"识别完成 · {len(items)} 件 · 已更新 · 按 {HOTKEY.upper()} 重试"))
        except Exception as e:
            q.put(("error", f"出错: {e}"))
        finally:
            busy.clear()

    def _unlock_if_stuck():
        # 看门狗：截图/OCR 可能挂起（尤其挂机/睡眠后），超时强制解锁，保证 F9 始终可用。
        if busy.is_set():
            q.put(("error", "识别超时（截图/OCR 卡住），已解锁热键，请重试"))
            busy.clear()

    def do_capture():
        # 热键回调必须立刻返回。若在此阻塞，keyboard 的监听线程被堵死，
        # 之后按 F9 就再也没反应（即「挂机太久按 F9 没反应」）。
        if busy.is_set():
            return
        busy.set()
        q.put(("status", "识别中..."))
        threading.Thread(target=capture_worker, daemon=True).start()
        threading.Timer(CAPTURE_TIMEOUT, _unlock_if_stuck).start()

    def poll():
        try:
            while True:
                msg = q.get_nowait()
                kind = msg[0]
                if kind == "status":
                    status.config(text=msg[1], fg="#8b93a1")
                elif kind == "result":
                    render_results(root, status, frame, fonts, msg[1], status_text=msg[2])
                elif kind == "error":
                    render_results(root, status, frame, fonts, [], err=msg[1])
        except queue.Empty:
            pass
        except Exception:
            pass   # 渲染出错也不能中断 poll，否则界面会卡死、看起来像没反应
        root.after(100, poll)

    try:
        import keyboard
        keyboard.add_hotkey(HOTKEY, do_capture)
    except Exception as e:
        render_results(root, status, frame, fonts, [],
                       err=f"热键注册失败（可能需要管理员权限）: {e}")

    status.config(text=f"就绪 · 按 {HOTKEY.upper()} 截图识别")
    root.after(100, poll)
    root.mainloop()


def main():
    args = sys.argv[1:]
    refresh = "--refresh" in args
    registry = ItemRegistry()
    registry.load(force_refresh=refresh)

    if "--image" in args:
        i = args.index("--image")
        path = args[i + 1]
        import numpy as np
        from PIL import Image
        img = np.asarray(Image.open(path).convert("RGB"))[:, :, ::-1]  # RGB->BGR
        results = process_once(registry, image=img)
        if not results:
            print("未识别到道具", flush=True)
            return
        for r in results:
            low = r["low_sell"] if r["low_sell"] is not None else "—"
            high = r["high_buy"] if r["high_buy"] is not None else "—"
            badge, _ = _vaulted_label(r["vaulted"])
            print(f"{r['zh']}  ({r['en']})  [{badge}]")
            print(f"    卖家最低: {low} 白金 | 买家最高: {high} 白金")
        return

    if refresh:
        print("缓存已更新，可正常启动（python wf_price_tool.py）。", flush=True)
        return

    run_gui(registry)


if __name__ == "__main__":
    main()
