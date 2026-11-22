"""AI analysis of Steam Workshop text (descriptions + comments).

The user supplies an OpenAI-compatible API key. Nothing is sent anywhere
except to the endpoint the user configures. Results are cached per mod so
re-opening the panel does not spend tokens again.
"""
from __future__ import annotations

import hashlib
import json
from urllib.parse import urlsplit
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import net

DEFAULT_BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
COMMENT_URL = "https://steamcommunity.com/comment/PublishedFile_Public/render/{appid}/{id}/"
MAX_COMMENTS = 40
MAX_DESCRIPTION = 6000
MAX_COMMENT_CHARS = 4000
CACHE_TTL = 6 * 3600

SYSTEM_PROMPT = (
    "你是《群星》(Stellaris) 与《钢铁雄心4》(HOI4) 的 MOD 兼容性分析助手。"
    "你会拿到四类信息：① 这个 MOD 的创意工坊标题与描述；② 评论摘录；"
    "③ 这个 MOD 的本地文件结构（descriptor 元数据、顶层目录、是否覆盖 common/、"
    "本地化语言、是否像兼容补丁）；④ 当前播放集的启用顺序与冲突统计。"
    "请只依据给出的信息判断，不要编造；没把握就把该字段留空或写“未知”。"
    "输出要求：verdict 只能是 compatible / needs_patch / incompatible / unknown 之一；"
    "position 是这个 MOD 应该排在第几位（从 1 开始，整数；没把握写 0）；"
    "neighbors 写“前面是谁”“后面是谁”（各一句）；requires 写必须同时启用的前置 MOD；"
    "load_order / compatibility / notices 各不超过 4 条、每条不超过 40 个汉字，"
    "末尾用括号标注依据是（描述）（评论）（文件）（顺序）。只输出 JSON。"
)

RESULT_SCHEMA = (
    '{"summary": "一句话总结", "verdict": "compatible|needs_patch|incompatible|unknown", '
    '"position": 0, "neighbors": ["前面：…", "后面：…"], "requires": ["…"], '
    '"load_order": ["…"], "compatibility": ["…"], "notices": ["…"]}'
)


class AiError(ValueError):
    """A setting or API problem whose message is safe to show to the user."""


def _text_of(value, limit):
    text = str(value or "").strip()
    return text[:limit]


class AiSettings:
    """API key / endpoint / model, stored next to the other user data."""

    def __init__(self, data_dir):
        self.path = Path(data_dir) / "ai-settings.json"
        self.api_key = ""
        self.base_url = DEFAULT_BASE
        self.model = DEFAULT_MODEL
        self.steam_cookie = ""
        self._load()

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        self.api_key = _text_of(data.get("api_key"), 400)
        self.base_url = _text_of(data.get("base_url"), 300) or DEFAULT_BASE
        self.model = _text_of(data.get("model"), 120) or DEFAULT_MODEL
        self.steam_cookie = _text_of(data.get("steam_cookie"), 2000)

    def save(self, api_key=None, base_url=None, model=None, steam_cookie=None):
        if api_key is not None:
            key_text = _text_of(api_key, 400)
            # 只有真填了内容才覆盖；清空走 clear()，避免一次空提交把密钥抹掉。
            if key_text:
                self.api_key = key_text
        if base_url is not None:
            self.base_url = _text_of(base_url, 300) or DEFAULT_BASE
        if model is not None:
            self.model = _text_of(model, 120) or DEFAULT_MODEL
        if steam_cookie is not None:
            self.steam_cookie = _text_of(steam_cookie, 2000)
        parts = urlsplit(self.base_url or "")
        if parts.scheme not in ("https", "http"):
            raise AiError("接口地址必须以 https:// 开头（本机可用 http://127.0.0.1）。")
        if parts.scheme == "http" and (parts.hostname or "") not in ("127.0.0.1", "localhost", "::1"):
            raise AiError("接口地址必须使用 HTTPS。")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"api_key": self.api_key, "base_url": self.base_url, "model": self.model,
                   "steam_cookie": self.steam_cookie}
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        return self.snapshot()

    def clear(self):
        # Clearing credentials must clear the Steam cookie too: leaving it behind
        # let people believe their Steam session data was gone while it was still
        # sitting in ai-settings.json.
        self.api_key = ""
        self.steam_cookie = ""
        self.save()
        return {**self.snapshot(), "message": "已清除 API Key 与 Steam Cookie"}

    def snapshot(self):
        return {
            "ok": True,
            "configured": bool(self.api_key),
            "has_key": bool(self.api_key),
            "base_url": self.base_url,
            "model": self.model,
            "has_steam_cookie": bool(self.steam_cookie),
            "provider": "openai-compatible",
        }


def strip_html(fragment):
    """Turn a Steam HTML fragment into readable plain text."""
    import html as html_module
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", str(fragment or ""))
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html_module.unescape(text)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_comments(page):
    """Extract comment bodies from the Steam comment render fragment."""
    text = str(page or "")
    found = re.findall(r'(?is)class="commentthread_comment_text"[^>]*>(.*?)</div>', text)
    if not found:
        found = re.findall(r'(?is)<div class="commenttext">(.*?)</div>\s*</div>', text)
    if not found:
        found = re.findall(r'(?is)class="commenttext"[^>]*>(.*?)</div>', text)
    out = []
    seen = set()
    for chunk in found:
        text = strip_html(chunk)
        text = re.sub(r"\s+", " ", text)[:600]
        if len(text) < 4 or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def fetch_comments(appid, mod_id, limit=MAX_COMMENTS, timeout=12, cookie=None):
    """Best effort comment fetch.

    Steam only serves workshop comments to a logged-in session, so without a
    ``steamLoginSecure`` cookie this returns [] immediately instead of
    hammering community endpoints for nothing.
    """
    if not cookie:
        return []
    if not str(mod_id or "").isdigit():
        return []
    url = COMMENT_URL.format(appid=appid, id=mod_id) + "?" + urllib.parse.urlencode(
        {"start": 0, "count": max(1, min(limit, MAX_COMMENTS))})
    request = urllib.request.Request(url, headers={
        "User-Agent": net.USER_AGENT,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://steamcommunity.com/sharedfiles/filedetails/?id=" + str(mod_id),
        "X-Requested-With": "XMLHttpRequest",
        "Cookie": cookie,
    })
    try:
        with net.open_url(request, timeout=timeout) as response:
            raw = response.read(512 * 1024)
    except (net.NetworkError, ValueError, OSError, urllib.error.HTTPError):
        return []
    text = raw.decode("utf-8", "replace").strip()
    fragment = text
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except ValueError:
            return []
        if not isinstance(payload, dict) or not payload.get("success"):
            return []
        fragment = str(payload.get("comments_html") or "")
    return parse_comments(fragment)[:limit]


def trim_for_prompt(text, head=2200, tail=800):
    """Keep the intro and the tail (usually the known-issues list)."""
    raw = str(text or "").strip()
    if len(raw) <= head + tail:
        return raw
    omitted = len(raw) - head - tail
    return raw[:head] + f"\n…[已省略 {omitted} 字符]…\n" + raw[-tail:]


def build_messages(name, description, comments, files=None, context=None):
    body = [f"MOD 标题：{name}"]
    body.append("描述：\n" + (description or "（无描述）"))
    if comments:
        joined = "\n".join(f"- {item}" for item in comments)
        body.append("评论摘录：\n" + joined)
    files = files or {}
    if files:
        desc = files.get("descriptor") or {}
        bits = []
        if desc.get("version"):
            bits.append("版本 " + str(desc["version"]))
        if desc.get("supported_version"):
            bits.append("支持游戏版本 " + str(desc["supported_version"]))
        if desc.get("tags"):
            bits.append("标签 " + ",".join(desc["tags"][:4]))
        if desc.get("dependencies"):
            bits.append("声明依赖 " + ",".join(desc["dependencies"][:6]))
        body.append("文件结构：" + ("；".join(bits) if bits else "无 descriptor 元数据"))
        body.append("顶层目录：" + (", ".join(files.get("top") or []) or "（空）"))
        if files.get("locales"):
            body.append("本地化语言：" + ", ".join(files["locales"]))
        if files.get("overrides"):
            body.append("覆盖的游戏目录：" + ", ".join(files["overrides"]))
        if files.get("patch_like"):
            body.append("疑似兼容补丁特征：" + ", ".join(files["patch_like"]))
        body.append("文件数 %s，主目录约 %s MB" % (files.get("files") or 0, files.get("size_mb") or 0))
        if files.get("error"):
            body.append("（注意：" + str(files["error"]) + "）")
    ctx = context or {}
    if ctx.get("order"):
        body.append("当前播放集启用顺序（第 N 位: 名称）：\n" + "\n".join(ctx["order"][:40]))
    if ctx.get("conflicts"):
        body.append("当前冲突统计：" + str(ctx["conflicts"]))
    if ctx.get("game_version"):
        body.append("游戏版本：" + str(ctx["game_version"]))
    body.append("请只输出这个 JSON：" + RESULT_SCHEMA)
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n\n".join(body)[:12000]},
    ]


def _verdict_of(value):
    text = _text_of(value, 20).lower()
    return text if text in ("compatible", "needs_patch", "incompatible", "unknown") else "unknown"


def _position_of(value):
    try:
        num = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, min(num, 999))


def scan_mod_files(mod):
    """Summarise a MOD's on-disk shape so the model can reason about real content."""
    import io
    import os
    out = {"dir": "", "files": 0, "size_mb": 0.0, "top": [], "descriptor": {},
           "locales": [], "overrides": [], "patch_like": [], "error": ""}
    path = str(mod.get("dir") or "")
    if not path or not os.path.isdir(path):
        out["error"] = "本地目录不可用"
        return out
    out["dir"] = path
    total = 0
    size = 0
    tops = []
    try:
        for entry in os.scandir(path):
            tops.append(entry.name)
            if entry.is_file():
                total += 1
                try:
                    size += entry.stat().st_size
                except OSError:
                    pass
        out["top"] = sorted(tops)[:24]
    except OSError as exc:
        out["error"] = "读取目录失败：%s" % exc
    out["files"] = total
    out["size_mb"] = round(size / 1048576.0, 1) if size else 0.0

    def read_text(rel, cap=4000):
        fp = os.path.join(path, rel)
        try:
            with io.open(fp, encoding="utf-8-sig", errors="replace") as fh:
                return fh.read(cap)
        except OSError:
            return ""

    desc = read_text("descriptor.mod") or read_text("descriptor.txt")
    for line in desc.splitlines():
        if "=" not in line:
            continue
        key, _eq, value = line.partition("=")
        key = key.strip().lower()
        value = value.strip().strip('"')
        if key in ("name", "version", "supported_version", "tags", "dependencies", "picture", "path"):
            if key in ("dependencies", "tags"):
                bucket = out["descriptor"].setdefault(key, [])
                for piece in value.strip().strip("{}").replace(",", " ").split():
                    piece = piece.strip().strip('"').strip("'")
                    if piece:
                        bucket.append(piece)
            else:
                out["descriptor"][key] = value[:80]
    loc = os.path.join(path, "localisation")
    if os.path.isdir(loc):
        for entry in os.scandir(loc):
            if entry.is_file():
                out["locales"].append(entry.name.rsplit(".", 1)[0][-6:])
        out["locales"] = sorted(set(out["locales"]))[:8]
    for name in ("common", "events", "gfx", "interface", "sound", "history", "map"):
        if name in out["top"]:
            out["overrides"].append(name)
    lowered = " ".join(out["top"]).lower()
    for mark in ("patch", "fix", "compat", "framework", "core", "lib"):
        if mark in lowered:
            out["patch_like"].append(mark)
    return out


def extract_json(text):
    """Parse the model output; tolerate code fences and stray prose."""
    raw = str(text or "").strip()
    if not raw:
        raise AiError("AI 返回了空内容。")
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    try:
        data = json.loads(raw)
    except ValueError:
        match = re.search(r"\{.*\}", raw, re.S)
        if not match:
            raise AiError("AI 返回的内容不是有效 JSON。")
        try:
            data = json.loads(match.group(0))
        except ValueError as exc:
            raise AiError("AI 返回的内容不是有效 JSON。") from exc
    if not isinstance(data, dict):
        raise AiError("AI 返回的内容格式不正确。")
    def rows(key):
        value = data.get(key)
        if not isinstance(value, list):
            return []
        out = []
        for item in value[:6]:
            text = _text_of(item if isinstance(item, str) else json.dumps(item, ensure_ascii=False), 160)
            if text:
                out.append(text)
        return out
    return {
        "summary": _text_of(data.get("summary"), 300),
        "load_order": rows("load_order"),
        "compatibility": rows("compatibility"),
        "notices": rows("notices"),
        "verdict": _verdict_of(data.get("verdict")),
        "position": _position_of(data.get("position")),
        "neighbors": rows("neighbors"),
        "requires": rows("requires"),
    }


class AiService:
    def __init__(self, data_dir, workshop=None, game_key="stellaris"):
        self.settings = AiSettings(data_dir)
        self.workshop = workshop
        self.game_key = game_key
        self.cache_path = Path(data_dir) / "ai-cache.json"
        self.cache = self._load_cache()

    # ---------------------------------------------------------- cache
    def _load_cache(self):
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8-sig"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_cache(self):
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self.cache, ensure_ascii=False)[:400000],
                                       encoding="utf-8")
        except OSError:
            pass

    # ---------------------------------------------------------- http
    def _chat(self, messages, timeout=90, max_tokens=900, json_mode=True):
        if not self.settings.api_key:
            raise AiError("还没有配置 AI 密钥，请先在“AI 设置”里填写。")
        base = self.settings.base_url.rstrip("/")
        url = base + "/chat/completions"
        payload = {
            "model": self.settings.model,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + self.settings.api_key,
        }
        request = urllib.request.Request(url, data=body, headers=headers)
        try:
            with net.open_url(request, timeout=timeout, raise_http=True) as response:
                raw = response.read(2 * 1024 * 1024)
        except net.NetworkError as exc:
            raise AiError(str(exc)) from exc
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read(600).decode("utf-8", "replace")
            except OSError:
                pass
            if exc.code in (401, 403):
                raise AiError(
                    "AI 接口拒绝了这个密钥（HTTP %s）。请确认：① 密钥是否填错或已失效；"
                    "② 接口地址与密钥是否属于同一家（DeepSeek 用 https://api.deepseek.com）；"
                    "③ 账号是否还有额度。可点“测试连接”验证。" % exc.code
                ) from exc
            if exc.code == 429:
                raise AiError("AI 接口限流了（HTTP 429），请稍后再试。") from exc
            raise AiError("AI 接口返回 HTTP %s。%s" % (exc.code, detail[:200])) from exc
        except (OSError, ValueError) as exc:
            raise AiError("调用 AI 接口失败，请稍后重试。") from exc
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except ValueError as exc:
            raise AiError("AI 接口返回的内容不是 JSON。") from exc
        choices = data.get("choices") or []
        if not choices:
            error = (data.get("error") or {}).get("message") if isinstance(data.get("error"), dict) else ""
            raise AiError("AI 接口没有返回结果。" + (str(error)[:160] if error else ""))
        message = choices[0].get("message") or {}
        return message.get("content") or "", data.get("usage") or {}

    # ------------------------------------------------------- analysis
    def _description(self, mod):
        text = trim_for_prompt(mod.get("workshop_description"))
        if text:
            return text
        steam_id = str(mod.get("steam_id") or "")
        if self.workshop and steam_id.isdigit():
            try:
                details = self.workshop.details([steam_id]) or {}
                item = details.get(steam_id) or {}
                return trim_for_prompt(item.get("description"))
            except (OSError, ValueError):
                return ""
        return ""

    def test_connection(self):
        """真的发一次最小请求，确认 key / 地址 / 模型都能用。"""
        if not self.settings.api_key:
            raise AiError("还没有配置 AI 密钥，请先在「AI 设置」里填写。")
        started = time.time()
        reply = self._chat([
            {"role": "system", "content": "只回复两个字：可用"},
            {"role": "user", "content": "ping"},
        ], timeout=25, max_tokens=8, json_mode=False)
        text = ""
        if isinstance(reply, dict):
            choices = reply.get("choices") or []
            if choices:
                text = ((choices[0].get("message") or {}).get("content") or "").strip()
        return {
            "ok": True,
            "model": self.settings.model,
            "base_url": self.settings.base_url,
            "latency_ms": int((time.time() - started) * 1000),
            "reply": text[:40],
        }

    def analyze_one(self, mod, scope="both", comment_limit=MAX_COMMENTS, use_cache=True):
        mod_id = str(mod.get("id") or "")
        name = _text_of(mod.get("workshop_title") or mod.get("name"), 120) or mod_id
        key = f"{self.game_key}:{mod_id}"
        if use_cache:
            cached = self.cache.get(key)
            if isinstance(cached, dict) and time.time() - (cached.get("at") or 0) < CACHE_TTL \
                    and cached.get("scope") == scope:
                payload = dict(cached.get("result") or {})
                payload["cached"] = True
                return payload
        description = self._description(mod) if scope in ("both", "description") else ""
        comments = []
        if scope in ("both", "comments"):
            appid = str(mod.get("appid") or "")
            steam_id = str(mod.get("steam_id") or "")
            if not appid:
                from . import games as games_module
                appid = str(games_module.get(self.game_key)["appid"])
            comments = fetch_comments(appid, steam_id, comment_limit,
                                      cookie=self.settings.steam_cookie)
        if not description and not comments:
            return {"mod_id": mod_id, "name": name, "ok": False,
                    "error": "没有可分析的文本（该 MOD 没有创意工坊描述或评论）。"}
        files = scan_mod_files(mod)
        context = mod.get("_context") if isinstance(mod.get("_context"), dict) else None
        content, usage = self._chat(build_messages(name, description, comments, files, context))
        result = extract_json(content)
        result.update({
            "mod_id": mod_id,
            "name": name,
            "ok": True,
            "source": {"description_chars": len(description), "comments": len(comments),
                       "files": files.get("files") or 0, "size_mb": files.get("size_mb") or 0},
            "usage": usage,
            "cached": False,
        })
        self.cache[key] = {"at": time.time(), "scope": scope, "result": result}
        return result

    SORT_SYSTEM = (
        "你是《群星》(Stellaris) 与《钢铁雄心4》(HOI4) 的 MOD 加载顺序专家。"
        "给你一组 MOD 的 id、名称、工坊描述摘要，请给出一个能正常工作的加载顺序："
        "框架与核心库 → 大型总览/兼容补丁 → 内容扩展 → 小型调整 → UI/汉化/音效。"
        "依赖别人的 MOD 必须排在它依赖的 MOD 之后。"
        "只输出 JSON：{\"order\": [\"id1\", \"id2\"], \"notes\": [\"一句中文说明\"]}，"
        "order 必须包含输入里的每一个 id，不增不减，不要解释。"
    )


    CONFLICT_SYSTEM = (
        "你是群星（Stellaris）与钢铁雄心4（HOI4）的 MOD 冲突诊断专家。"
        "用户会给你一批『文件覆盖冲突』：每条含文件路径、被哪些 MOD 覆盖、各 MOD 在播放集里的加载序（数字越大越晚加载、越晚加载者胜出）。"
        "请逐条判断：这条冲突是真问题还是正常覆盖，影响有多严重，以及处置建议。"
        "判断依据：路径属于 common/ 定义文件、events/、gfx/、interface/、localisation/ 时语义不同；"
        "同一个 MOD 内部的多份副本不算冲突；只有加载序相邻、且内容语义会互相覆盖时才需要处理。"
        "禁止编造文件内容；只能基于给出的路径与 MOD 名推理。"
        "输出必须是 JSON：{\"summary\": \"一句话总体结论\", "
        "\"items\": [{\"path\": \"原样路径\", \"severity\": \"critical|warning|info\", "
        "\"verdict\": \"keep|reorder|disable|patch|ignore\", \"advice\": \"≤40 字中文建议\", "
        "\"mods\": [\"涉及的 MOD 名\"]}], "
        "\"actions\": [{\"type\": \"disable|move|patch\", \"mod\": \"MOD 名\", \"reason\": \"≤30 字\"}]}。"
        "items 最多 12 条，按严重度排序；actions 最多 6 条。"
    )

    def sort_mods(self, mods):
        """让模型给一份加载顺序；返回 (按建议排好的 mod 列表, 备注列表)。"""
        snapshot = self.settings.snapshot()
        if not snapshot.get("configured"):
            raise AiError("还没有配置 AI 密钥，请先在「AI 设置」里填写。")
        if not mods:
            raise AiError("当前播放集没有启用的 MOD。")
        items = []
        for mod in mods:
            desc = (mod.get("description") or mod.get("workshop_description") or "").strip()
            items.append({
                "id": str(mod.get("id")),
                "name": (mod.get("name") or "")[:120],
                "desc": desc[:280],
            })
        payload = json.dumps(items, ensure_ascii=False)
        messages = [
            {"role": "system", "content": self.SORT_SYSTEM},
            {"role": "user", "content": "MOD 列表（JSON）：\n" + payload[:24000]},
        ]
        raw = self._chat(messages, timeout=120, max_tokens=2600, json_mode=True)
        # 注意：extract_json() 会归一化成 analyze 的固定 schema（summary/load_order/...），
        # 会丢掉 order 字段，所以这里自己解析原始 JSON。
        text = str(raw or "").strip()
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
        try:
            data = json.loads(text)
        except ValueError:
            match = re.search(r"\{.*\}", text, re.S)
            if not match:
                raise AiError("AI 返回的内容不是有效 JSON，请重试。")
            try:
                data = json.loads(match.group(0))
            except ValueError as exc:
                raise AiError("AI 返回的内容不是有效 JSON，请重试。") from exc
        order = data.get("order") if isinstance(data, dict) else None
        notes = data.get("notes") if isinstance(data, dict) else None
        if not isinstance(order, list):
            raise AiError("AI 没有返回可用的排序结果，请重试。")
        by_id = {str(m.get("id")): m for m in mods}
        ordered = []
        seen = set()
        for item in order:
            key = str(item)
            if key in by_id and key not in seen:
                ordered.append(by_id[key])
                seen.add(key)
        for mod in mods:  # 模型漏掉的按原顺序补在末尾
            key = str(mod.get("id"))
            if key not in seen:
                ordered.append(mod)
                seen.add(key)
        clean_notes = []
        if isinstance(notes, list):
            for note in notes[:6]:
                text = str(note).strip()
                if text:
                    clean_notes.append(text[:80])
        return ordered, clean_notes


    RULES_SYSTEM = """你是 Stellaris / Hearts of Iron IV 的 MOD 排序助手。
只根据给出的 MOD 名称与说明文字，抽取「排序要求」与「兼容性声明」，不要推断、不要补充常识。
只输出 JSON：{"rules":[{"mod":"MOD 名称","kind":"top|bottom|before|after|incompatible|requires","target":"另一个 MOD 名称（top/bottom 留空）","quote":"说明原文片段，最多 60 字","confidence":0.0-1.0}],"notes":["最多 6 条"]}
判定标准：
- top：说明写了「必须放最上面 / load first / must be at the top / 放到最前」。
- bottom：说明写了「放最下面 / load last / must be at the bottom / 放到最后」。
- before / after：说明写了「在 X 之前加载 / load before X / 需要 X 之后 / load after X」。
- incompatible：说明写了「与 X 不兼容 / incompatible with X / 不要与 X 同时使用」。
- requires：说明写了「需要 X / requires X / 依赖 X」。
- 说明里没有明确写的，一律不要输出；一句话只能证明一条规则。
- 中文与英文都要识别；quote 必须来自原文，不要改写。
- 最多 20 条，按 confidence 从高到低。"""

    def extract_rules(self, mods, force=False, batch=12, min_confidence=0.45):
        """从 MOD 说明里抽排序规则；返回 (rules, notes, 缓存命中数)。"""
        snapshot = self.settings.snapshot()
        if not snapshot.get("configured"):
            raise AiError("还没有配置 AI 密钥，请先在「AI 设置」里填写。")
        if not mods:
            raise AiError("当前播放集没有 MOD。")
        cache_path = Path(self.settings.path.parent) / "rules-cache.json"
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8-sig"))
            if not isinstance(cache, dict):
                cache = {}
        except (OSError, ValueError):
            cache = {}
        cache = {} if force else cache

        prepared = []
        for mod in mods:
            desc = (mod.get("description") or mod.get("workshop_description") or "").strip()
            desc = strip_html(desc)[:600]
            key = str(mod.get("id"))
            stamp = hashlib.sha1(((mod.get("name") or "") + "\n" + desc).encode("utf-8")).hexdigest()[:16]
            hit = cache.get(key)
            if isinstance(hit, dict) and hit.get("stamp") == stamp and isinstance(hit.get("rules"), list):
                prepared.append((mod, desc, stamp, hit["rules"]))
            else:
                prepared.append((mod, desc, stamp, None))

        missing = [(mod, desc, stamp) for mod, desc, stamp, rules in prepared if rules is None]
        fetched = {}
        for start in range(0, len(missing), batch):
            chunk = missing[start:start + batch]
            items = []
            for mod, desc, _stamp in chunk:
                items.append({
                    "id": str(mod.get("id")),
                    "name": (mod.get("name") or "")[:120],
                    "desc": desc,
                })
            messages = [
                {"role": "system", "content": self.RULES_SYSTEM},
                {"role": "user", "content": "MOD 列表（JSON）：\n" + json.dumps(items, ensure_ascii=False)[:20000]},
            ]
            raw = self._chat(messages, timeout=120, max_tokens=1800, json_mode=True)
            text = str(raw or "").strip()
            text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
            try:
                data = json.loads(text)
            except ValueError:
                match = re.search(r"\{.*\}", text, re.S)
                if not match:
                    continue
                try:
                    data = json.loads(match.group(0))
                except ValueError:
                    continue
            rows = data.get("rules") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                continue
            by_name = {}
            for mod, _desc, _stamp in chunk:
                by_name[(mod.get("name") or "").strip().casefold()] = str(mod.get("id"))
                by_name[str(mod.get("id"))] = str(mod.get("id"))
            for row in rows:
                if not isinstance(row, dict):
                    continue
                owner = by_name.get(str(row.get("mod") or "").strip().casefold())
                if not owner:
                    continue
                kind = str(row.get("kind") or "").strip().lower()
                if kind not in ("top", "bottom", "before", "after", "incompatible", "requires"):
                    continue
                try:
                    confidence = float(row.get("confidence", 0.7))
                except (TypeError, ValueError):
                    confidence = 0.7
                if confidence < min_confidence:
                    continue
                fetched.setdefault(owner, []).append({
                    "mod_id": owner,
                    "mod": str(row.get("mod") or "")[:120],
                    "kind": kind,
                    "target": str(row.get("target") or "")[:120],
                    "quote": str(row.get("quote") or "")[:80],
                    "confidence": round(max(0.0, min(1.0, confidence)), 2),
                })

        rules = []
        hits = 0
        for mod, _desc, stamp, cached_rules in prepared:
            key = str(mod.get("id"))
            if cached_rules is None:
                cached_rules = fetched.get(key, [])
                cache[key] = {"stamp": stamp, "rules": cached_rules}
            else:
                hits += 1
            for row in cached_rules:
                row = dict(row)
                row["mod_id"] = key
                row["mod"] = mod.get("name") or row.get("mod") or ""
                rules.append(row)
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(cache, ensure_ascii=False)[:400000], encoding="utf-8")
        except OSError:
            pass
        notes = []
        for row in rules[:6]:
            notes.append(f"{row.get('mod')}：{row.get('kind')} {row.get('target')}".strip())
        return rules, notes, hits

    CRASH_SYSTEM = """你是 Stellaris / Hearts of Iron IV 的崩溃归因助手。
只根据给出的 error.log 报错统计、样例行与冲突统计，判断最可能是哪个 MOD 引起问题，并给出可执行的处理顺序。
只输出 JSON：{"verdict":"mod|conflict|load_order|unknown","summary":"一句话结论，最多 80 字","suspects":[{"mod":"MOD 名称","reason":"为什么怀疑它，最多 60 字","confidence":0.0-1.0}],"fixes":["按顺序执行的处理步骤，最多 6 条，每条最多 60 字"]}
要求：
- suspects 最多 3 个，按 confidence 从高到低；证据不足就少写或写空数组。
- 报错行里带 MOD 路径的优先；其次是「定义冲突」里被覆盖的那个 MOD。
- 不要建议删除游戏文件；不要编造日志里没有的东西。"""

    def explain_crash(self, report):
        """根据崩溃/报错统计给出归因与处理建议。"""
        snapshot = self.settings.snapshot()
        if not snapshot.get("configured"):
            raise AiError("还没有配置 AI 密钥，请先在「AI 设置」里填写。")
        if not isinstance(report, dict):
            raise AiError("没有可分析的崩溃报告。")
        suspects = report.get("mapped") or []
        if not suspects and not report.get("unmapped"):
            raise AiError("日志里没有找到可归因的报错，先跑一次游戏再来。")
        lines = []
        for row in suspects[:8]:
            kinds = "、".join("%s×%s" % (k, v) for k, v in list((row.get("kinds") or {}).items())[:4])
            lines.append("MOD：%s｜报错 %s 条｜类型：%s" % (row.get("name"), row.get("count"), kinds or "未分类"))
            for sample in (row.get("samples") or [])[:3]:
                lines.append("  样例：" + str(sample)[:200])
        if report.get("unmapped"):
            lines.append("未归属到 MOD 的报错 %s 条，例如：" % len(report["unmapped"]))
            for row in report["unmapped"][:3]:
                lines.append("  " + str(row.get("line"))[:200])
        if report.get("dumps"):
            lines.append("崩溃转储文件：" + "、".join(str(name) for name in report["dumps"][:4]))
        messages = [
            {"role": "system", "content": self.CRASH_SYSTEM},
            {"role": "user", "content": "报错统计（来自 error.log）：\n" + "\n".join(lines)[:14000]},
        ]
        raw = self._chat(messages, timeout=90, max_tokens=1200, json_mode=True)
        text = str(raw or "").strip()
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
        try:
            data = json.loads(text)
        except ValueError:
            match = re.search(r"\{.*\}", text, re.S)
            if not match:
                raise AiError("AI 返回的内容不是有效 JSON，请重试。")
            try:
                data = json.loads(match.group(0))
            except ValueError as exc:
                raise AiError("AI 返回的内容不是有效 JSON，请重试。") from exc
        if not isinstance(data, dict):
            raise AiError("AI 没有返回可用的归因结果，请重试。")

        def _text(value, limit):
            return str(value or "").strip()[:limit]

        verdict = str(data.get("verdict") or "unknown").strip().lower()
        if verdict not in ("mod", "conflict", "load_order", "unknown"):
            verdict = "unknown"
        rows = []
        for item in (data.get("suspects") or [])[:3]:
            if not isinstance(item, dict):
                continue
            try:
                confidence = float(item.get("confidence", 0.5))
            except (TypeError, ValueError):
                confidence = 0.5
            rows.append({
                "mod": _text(item.get("mod"), 120),
                "reason": _text(item.get("reason"), 160),
                "confidence": round(max(0.0, min(1.0, confidence)), 2),
            })
        fixes = [_text(item, 160) for item in (data.get("fixes") or [])[:6] if _text(item, 160)]
        return {
            "verdict": verdict,
            "summary": _text(data.get("summary"), 200),
            "suspects": rows,
            "fixes": fixes,
            "model": self.settings.model,
            "scanned": len(suspects),
        }

    def analyze_conflicts(self, conflicts, order=None, limit=40):
        """让模型解读文件覆盖冲突；返回 {summary, items, actions, scanned}。"""
        snapshot = self.settings.snapshot()
        if not snapshot.get("configured"):
            raise AiError("还没有配置 AI 密钥，请先在「AI 设置」里填写。")
        rows = []
        for item in (conflicts or [])[:limit]:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or item.get("key") or "").strip()
            if not path:
                continue
            holders = []
            for mod in (item.get("mods") or [])[:8]:
                if isinstance(mod, dict):
                    holders.append({
                        "name": str(mod.get("name") or "")[:80],
                        "rank": mod.get("rank") if isinstance(mod.get("rank"), int) else None,
                        "winner": bool(mod.get("winner")),
                    })
                elif mod:
                    holders.append({"name": str(mod)[:80], "rank": None, "winner": False})
            rows.append({
                "path": path[:200],
                "kind": str(item.get("kind") or "")[:24],
                "holders": holders,
                "winner": str(item.get("winner") or "")[:80],
            })
        if not rows:
            raise AiError("当前没有可分析的冲突（扫描结果为空）。")
        payload = {
            "conflicts": rows,
            "order": [str(x)[:80] for x in (order or [])[:60]],
        }
        messages = [
            {"role": "system", "content": self.CONFLICT_SYSTEM},
            {"role": "user", "content": "冲突清单（JSON）：\n" + json.dumps(payload, ensure_ascii=False)[:24000]},
        ]
        raw = self._chat(messages, timeout=120, max_tokens=2400, json_mode=True)
        text = str(raw or "").strip()
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
        try:
            data = json.loads(text)
        except ValueError:
            match = re.search(r"\{.*\}", text, re.S)
            if not match:
                raise AiError("AI 返回的内容不是有效 JSON，请重试。")
            try:
                data = json.loads(match.group(0))
            except ValueError as exc:
                raise AiError("AI 返回的内容不是有效 JSON，请重试。") from exc
        if not isinstance(data, dict):
            raise AiError("AI 没有返回可用的冲突结论，请重试。")

        def _pick(value, allowed, fallback):
            text_v = str(value or "").strip().lower()
            return text_v if text_v in allowed else fallback

        items = []
        for item in (data.get("items") or [])[:12]:
            if not isinstance(item, dict):
                continue
            items.append({
                "path": str(item.get("path") or "")[:200],
                "severity": _pick(item.get("severity"), {"critical", "warning", "info"}, "info"),
                "verdict": _pick(item.get("verdict"), {"keep", "reorder", "disable", "patch", "ignore"}, "keep"),
                "advice": str(item.get("advice") or "")[:120],
                "mods": [str(x)[:80] for x in (item.get("mods") or [])[:6] if x],
            })
        actions = []
        for item in (data.get("actions") or [])[:6]:
            if not isinstance(item, dict):
                continue
            actions.append({
                "type": _pick(item.get("type"), {"disable", "move", "patch"}, "patch"),
                "mod": str(item.get("mod") or "")[:80],
                "reason": str(item.get("reason") or "")[:80],
            })
        return {
            "summary": str(data.get("summary") or "")[:300],
            "items": items,
            "actions": actions,
            "scanned": len(rows),
            "model": snapshot.get("model"),
        }

    def analyze(self, mods, scope="both", comment_limit=MAX_COMMENTS, use_cache=True):
        if not self.settings.api_key:
            raise AiError("还没有配置 AI 密钥，请先在“AI 设置”里填写。")
        items = []
        for mod in mods[:8]:
            try:
                items.append(self.analyze_one(mod, scope=scope,
                                             comment_limit=comment_limit, use_cache=use_cache))
            except AiError as exc:
                items.append({"mod_id": str(mod.get("id") or ""),
                              "name": _text_of(mod.get("name"), 120),
                              "ok": False, "error": str(exc)})
            time.sleep(0.2)
        self._save_cache()
        return {"ok": True, "model": self.settings.model, "items": items}
