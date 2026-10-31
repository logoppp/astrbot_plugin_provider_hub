from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.star import Context, Star, register
from astrbot.api.web import request as web_request

PLUGIN_NAME = "astrbot_plugin_provider_hub"

PLUGIN_DIR = Path(__file__).resolve().parent
DATA_DIR = PLUGIN_DIR.parent.parent
PLUGINS_DIR = DATA_DIR / "plugins"
CONFIG_DIR = DATA_DIR / "config"
CMD_CONFIG = DATA_DIR / "cmd_config.json"

# _conf_schema.json 里 _special 取值 -> 供应商类别
PROVIDER_SPECIALS: dict[str, str] = {
    "select_provider": "chat",
    "select_provider_embedding": "embedding",
    "select_provider_rerank": "rerank",
    "select_provider_tts": "tts",
    "select_provider_stt": "stt",
}

KIND_LABEL = {
    "chat": "对话模型",
    "embedding": "向量模型",
    "rerank": "重排模型",
    "tts": "语音合成",
    "stt": "语音识别",
}

EXCLUDED_PLUGINS = {PLUGIN_NAME}

# 批量保存一次最多接受的条目数，防止误提交超大数组
MAX_BATCH_ITEMS = 200


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8-sig") as handle:
        return json.load(handle)


def _provider_kind(item: dict) -> str:
    ptype = str(item.get("type") or "").strip().lower()
    if not ptype:
        return "chat"
    if "embedding" in ptype:
        return "embedding"
    if "rerank" in ptype:
        return "rerank"
    if "tts" in ptype:
        return "tts"
    if "whisper" in ptype or "stt" in ptype or "speech_to_text" in ptype:
        return "stt"
    return "chat"


def _provider_brief(item: dict) -> dict:
    model = (
        item.get("model")
        or item.get("embedding_model")
        or item.get("type")
        or ""
    )
    return {
        "id": str(item.get("id") or ""),
        "model": str(model),
        "kind": _provider_kind(item),
        "enable": bool(item.get("enable", True)),
    }


def _walk_schema(node: Any, prefix: str = "") -> list[dict]:
    fields: list[dict] = []
    if not isinstance(node, dict):
        return fields
    for key, val in node.items():
        if not isinstance(val, dict):
            continue
        path = f"{prefix}.{key}" if prefix else key
        special = val.get("_special")
        if special in PROVIDER_SPECIALS:
            fields.append(
                {
                    "path": path,
                    "kind": PROVIDER_SPECIALS[special],
                    "special": special,
                    "label": str(val.get("description") or key).strip(),
                    "hint": str(val.get("hint") or "").strip(),
                }
            )
        nested = val.get("items")
        if isinstance(nested, dict):
            fields.extend(_walk_schema(nested, path))
    return fields


def _get_path(data: Any, path: str) -> Any:
    cursor = data
    for part in path.split("."):
        if not isinstance(cursor, dict) or part not in cursor:
            return None
        cursor = cursor[part]
    return cursor


def _set_path(data: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    cursor = data
    for part in parts[:-1]:
        nxt = cursor.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cursor[part] = nxt
        cursor = nxt
    cursor[parts[-1]] = value


def _display_name(plugin_dir: Path, fallback: str) -> str:
    meta = plugin_dir / "metadata.yaml"
    if not meta.is_file():
        return fallback
    try:
        for line in meta.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("display_name:"):
                value = line.split(":", 1)[1].strip().strip("'\"")
                return value or fallback
    except OSError:
        pass
    return fallback


def _backup_path(cfg_path: Path) -> Path:
    """同一秒内多次保存也不覆盖：重名时追加序号。"""
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup = cfg_path.with_name(f"{cfg_path.name}.bak_hub_{stamp}")
    index = 1
    while backup.exists():
        backup = cfg_path.with_name(f"{cfg_path.name}.bak_hub_{stamp}_{index}")
        index += 1
    return backup


@register(
    PLUGIN_NAME,
    "logoppp",
    "统一查看与修改所有引用官方供应商的插件配置",
    "v1.1.0",
)
class ProviderHubPlugin(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)

    async def initialize(self) -> None:
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/provider-hub/state",
            self.get_state,
            ["GET"],
            "供应商配置总览",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/provider-hub/save",
            self.save,
            ["POST"],
            "保存供应商配置",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/provider-hub/save-batch",
            self.save_batch,
            ["POST"],
            "批量保存供应商配置",
        )
        logger.info("[provider-hub] 已注册 /api/plug/%s/provider-hub/*", PLUGIN_NAME)

    async def get_state(self) -> dict:
        providers: list[dict] = []
        try:
            cfg = _load_json(CMD_CONFIG)
            for item in cfg.get("provider") or []:
                if isinstance(item, dict):
                    providers.append(_provider_brief(item))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[provider-hub] 读取官方 provider 失败: %s", exc)

        groups: list[dict] = []
        if PLUGINS_DIR.is_dir():
            for plugin_dir in sorted(PLUGINS_DIR.iterdir(), key=lambda p: p.name.lower()):
                if not plugin_dir.is_dir():
                    continue
                name = plugin_dir.name
                if name in EXCLUDED_PLUGINS:
                    continue
                schema_path = plugin_dir / "_conf_schema.json"
                if not schema_path.is_file():
                    continue
                try:
                    schema = _load_json(schema_path)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[provider-hub] 解析 %s schema 失败: %s", name, exc)
                    continue
                fields = _walk_schema(schema)
                if not fields:
                    continue

                cfg_path = CONFIG_DIR / f"{name}_config.json"
                current: Any = {}
                if cfg_path.is_file():
                    try:
                        current = _load_json(cfg_path)
                    except Exception:  # noqa: BLE001
                        current = {}
                rows = []
                for field in fields:
                    raw = _get_path(current, field["path"])
                    rows.append(
                        {
                            **field,
                            "value": "" if raw is None else str(raw),
                        }
                    )
                groups.append(
                    {
                        "plugin": name,
                        "display_name": _display_name(plugin_dir, name),
                        "fields": rows,
                    }
                )

        for group in groups:
            for field in group["fields"]:
                field["kind_label"] = KIND_LABEL.get(field["kind"], field["kind"])

        return {
            "status": "ok",
            "data": {
                "providers": providers,
                "groups": groups,
                "kinds": KIND_LABEL,
            },
        }

    async def save(self) -> dict:
        payload = await web_request.json(default={}) or {}
        plugin = str(payload.get("plugin") or "").strip()
        path = str(payload.get("path") or "").strip()
        value = payload.get("value")
        value = "" if value is None else str(value)

        if not plugin or not path:
            return {"status": "error", "message": "缺少 plugin 或 path"}

        plugin_dir = PLUGINS_DIR / plugin
        if not plugin_dir.is_dir() or plugin not in {
            item.name for item in PLUGINS_DIR.iterdir() if item.is_dir()
        }:
            return {"status": "error", "message": "插件不存在"}

        schema_path = plugin_dir / "_conf_schema.json"
        if not schema_path.is_file():
            return {"status": "error", "message": "该插件没有配置 schema"}
        try:
            allowed = {field["path"] for field in _walk_schema(_load_json(schema_path))}
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "message": f"解析 schema 失败: {exc}"}
        if path not in allowed:
            return {"status": "error", "message": "该字段不是官方供应商引用项"}

        cfg_path = CONFIG_DIR / f"{plugin}_config.json"
        if not cfg_path.is_file():
            return {"status": "error", "message": "配置文件不存在"}
        try:
            data = _load_json(cfg_path)
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "message": f"读取配置失败: {exc}"}

        backup = _backup_path(cfg_path)
        try:
            shutil.copy2(cfg_path, backup)
            _set_path(data, path, value)
            cfg_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "message": f"写入配置失败: {exc}"}

        reload_ok, reload_error = await self._reload_plugin(plugin)
        logger.info(
            "[provider-hub] %s.%s -> %r (backup=%s reload=%s)",
            plugin,
            path,
            value,
            backup.name,
            reload_ok,
        )
        return {
            "status": "ok",
            "data": {
                "plugin": plugin,
                "path": path,
                "value": value,
                "backup": backup.name,
                "reload_ok": reload_ok,
                "reload_error": reload_error,
            },
        }

    async def save_batch(self) -> dict:
        """一次提交本次所有改动：同一插件只写一次文件、只重载一次。

        请求体：{"items": [{"plugin": "...", "path": "...", "value": "..."}, ...]}
        """
        payload = await web_request.json(default={}) or {}
        raw_items = payload.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            return {"status": "error", "message": "items 不能为空"}
        if len(raw_items) > MAX_BATCH_ITEMS:
            return {"status": "error", "message": f"一次最多保存 {MAX_BATCH_ITEMS} 项"}

        grouped: dict[str, list[dict]] = {}
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            plugin = str(raw.get("plugin") or "").strip()
            path = str(raw.get("path") or "").strip()
            if not plugin or not path:
                continue
            value = raw.get("value")
            grouped.setdefault(plugin, []).append(
                {"path": path, "value": "" if value is None else str(value)}
            )

        if not grouped:
            return {"status": "error", "message": "没有有效的修改项"}

        results = []
        for plugin, changes in grouped.items():
            results.append(await self._apply_plugin_changes(plugin, changes))

        item_count = sum(len(changes) for changes in grouped.values())
        ok_count = sum(1 for item in results if item.get("ok"))
        logger.info(
            "[provider-hub] 批量保存 %s 个插件 / %s 项，成功 %s 个插件",
            len(grouped),
            item_count,
            ok_count,
        )
        return {
            "status": "ok",
            "data": {
                "results": results,
                "ok_count": ok_count,
                "plugin_count": len(grouped),
                "item_count": item_count,
            },
        }

    async def _apply_plugin_changes(self, plugin: str, changes: list[dict]) -> dict:
        plugin_dir = PLUGINS_DIR / plugin
        if not plugin_dir.is_dir():
            return {"plugin": plugin, "display_name": plugin, "ok": False, "error": "插件不存在"}
        display = _display_name(plugin_dir, plugin)

        schema_path = plugin_dir / "_conf_schema.json"
        if not schema_path.is_file():
            return {
                "plugin": plugin,
                "display_name": display,
                "ok": False,
                "error": "该插件没有配置 schema",
            }
        try:
            allowed = {field["path"] for field in _walk_schema(_load_json(schema_path))}
        except Exception as exc:  # noqa: BLE001
            return {
                "plugin": plugin,
                "display_name": display,
                "ok": False,
                "error": f"解析 schema 失败: {exc}",
            }

        cfg_path = CONFIG_DIR / f"{plugin}_config.json"
        if not cfg_path.is_file():
            return {
                "plugin": plugin,
                "display_name": display,
                "ok": False,
                "error": "配置文件不存在",
            }
        try:
            data = _load_json(cfg_path)
        except Exception as exc:  # noqa: BLE001
            return {
                "plugin": plugin,
                "display_name": display,
                "ok": False,
                "error": f"读取配置失败: {exc}",
            }

        applied: list[str] = []
        rejected: list[dict] = []
        for change in changes:
            path = change["path"]
            if path not in allowed:
                rejected.append({"path": path, "reason": "不是官方供应商引用项"})
                continue
            _set_path(data, path, change["value"])
            applied.append(path)

        if not applied:
            return {
                "plugin": plugin,
                "display_name": display,
                "ok": False,
                "error": "没有可写入的字段",
                "rejected": rejected,
            }

        backup = _backup_path(cfg_path)
        try:
            shutil.copy2(cfg_path, backup)
            cfg_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "plugin": plugin,
                "display_name": display,
                "ok": False,
                "error": f"写入配置失败: {exc}",
            }

        reload_ok, reload_error = await self._reload_plugin(plugin)
        logger.info(
            "[provider-hub] %s 批量写入 %s 项 (backup=%s reload=%s)",
            plugin,
            len(applied),
            backup.name,
            reload_ok,
        )
        return {
            "plugin": plugin,
            "display_name": display,
            "ok": True,
            "saved": len(applied),
            "paths": applied,
            "rejected": rejected,
            "backup": backup.name,
            "reload_ok": reload_ok,
            "reload_error": reload_error,
        }

    async def _reload_plugin(self, name: str) -> tuple[bool, str | None]:
        if name == PLUGIN_NAME:
            return True, None
        manager = getattr(self.context, "_star_manager", None)
        if manager is None:
            return False, "star manager unavailable"
        try:
            result = await manager.reload(name)
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        if isinstance(result, tuple):
            ok = bool(result[0]) if result else False
            err = result[1] if len(result) > 1 else None
            return ok, None if err is None else str(err)
        return bool(result), None
