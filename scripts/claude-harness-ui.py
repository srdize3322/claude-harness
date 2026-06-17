#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

MODEL_CACHE_TTL_SECONDS = int(os.environ.get("CLAUDE_HARNESS_CACHE_TTL", "300"))

HOME = Path.home()
ROOT = Path(__file__).resolve().parent

HARNESS_CONFIG_DIR = HOME / ".config" / "claude-harness"
HARNESS_ENV_FILE = HARNESS_CONFIG_DIR / ".env"
HARNESS_CACHE_FILE = HARNESS_CONFIG_DIR / "model-cache.json"
HARNESS_FAVORITES_FILE = HARNESS_CONFIG_DIR / "favorites.json"
OPENROUTER_SHARED_ENV_FILE = HOME / ".config" / "mcp-openrouter" / ".env"
OPENROUTER_PROFILE_ENV_FILE = HOME / ".config" / "claude-openrouter" / ".env"
MINIMAX_ENV_FILE = HOME / ".config" / "minimax-mcp" / ".env"
CLAUDE_STATE_FILE = HOME / ".claude.json"
OPENCODE_AUTH_FILE = HOME / ".local" / "share" / "opencode" / "auth.json"
CODEX_MODELS_FILE = HOME / ".codex" / "models_cache.json"
OPENCODE_MODELS_CACHE = HOME / ".cache" / "opencode" / "models.json"
MODELS_DEV_CACHE = HARNESS_CONFIG_DIR / "models-catalog.json"
MODELS_DEV_URL = "https://models.dev/api.json"
MODELS_DEV_TTL = int(os.environ.get("CLAUDE_HARNESS_CATALOG_TTL", "3600"))

CLAUDE_EFFORT_LEVELS = ["minimal", "low", "medium", "high", "xhigh", "max", "ultracode"]

THINKING_BUDGET_TOKENS = {
    "minimal": 1024,
    "low": 2048,
    "medium": 8192,
    "high": 16384,
    "xhigh": 32768,
    "max": 32768,
}

ADAPTIVE_EFFORT_MAP = {
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
    "ultracode": "xhigh",
}

ADAPTIVE_NO_XHIGH_MAP = {
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "max",
    "max": "max",
    "ultracode": "max",
}

TOGGLE_DEFAULT_BUDGET = 8192

CLAUDE_NATIVE_LAUNCHER = str(ROOT / "claude-native")
CLAUDE_OPENROUTER_LAUNCHER = str(ROOT / "claude-openrouter")
CLAUDE_OPENCODE_GO_LAUNCHER = str(ROOT / "claude-opencode-go")
CLAUDE_MINIMAX_LAUNCHER = str(ROOT / "claude-minimax")
CLAUDE_CODEX_LAUNCHER = str(ROOT / "claude-codex")

ESC = ""
BOLD = ""
DIM = ""
GREEN = ""
YELLOW = ""
BLUE = ""
RED = ""
RST = ""
W = 60


@dataclass
class ModelItem:
    model_id: str
    label: str
    reasoning: bool = False
    reasoning_options: list[dict] | None = None


@dataclass
class ProviderStatus:
    logged_in: bool
    detail: str


@dataclass
class ProviderDefinition:
    provider_id: str
    label: str
    launcher: str
    family: str
    supports_claude_launch: bool = True
    launch_block_reason: str | None = None
    experimental: bool = False
    default_model_env: str | None = None


@dataclass
class PermissionOption:
    label: str
    args: list[str]


@dataclass
class AgentSlots:
    opus: str | None = None
    sonnet: str | None = None
    haiku: str | None = None


def get_provider_models_for_slots(provider_id: str) -> list[ModelItem]:
    if provider_id == "minimax":
        return MINIMAX_MODELS
    if provider_id == "opencode-go":
        return OPENCODE_GO_MODELS
    for p in PROVIDERS:
        if p.provider_id == provider_id:
            return fetch_models_for_provider(p, force_refresh=False)
    return []


PROVIDERS = [
    ProviderDefinition("claude", "Claude nativo", CLAUDE_NATIVE_LAUNCHER, "claude",
                       default_model_env="CLAUDE_HARNESS_CLAUDE_MODEL"),
    ProviderDefinition("openrouter", "OpenRouter", CLAUDE_OPENROUTER_LAUNCHER, "claude",
                       experimental=True, default_model_env="CLAUDE_HARNESS_OPENROUTER_MODEL"),
    ProviderDefinition("opencode-go", "OpenCode Go", CLAUDE_OPENCODE_GO_LAUNCHER, "claude",
                       default_model_env="CLAUDE_HARNESS_OPENCODE_GO_MODEL"),
    ProviderDefinition("minimax", "MiniMax", CLAUDE_MINIMAX_LAUNCHER, "claude",
                       default_model_env="CLAUDE_HARNESS_MINIMAX_MODEL"),
    ProviderDefinition("codex", "Codex", CLAUDE_CODEX_LAUNCHER, "codex",
                       default_model_env="CLAUDE_HARNESS_CODEX_MODEL"),
]

PERMISSION_OPTIONS: dict[str, list[PermissionOption]] = {
    "claude": [
        PermissionOption("Default", []),
        PermissionOption("Accept edits", ["--permission-mode", "acceptEdits"]),
        PermissionOption("Auto", ["--permission-mode", "auto"]),
        PermissionOption("Plan", ["--permission-mode", "plan"]),
        PermissionOption("Dangerously skip permissions", ["--dangerously-skip-permissions"]),
    ],
    "codex": [
        PermissionOption("Default", []),
        PermissionOption("On request", ["-a", "on-request"]),
        PermissionOption("Never ask", ["-a", "never"]),
        PermissionOption("Dangerously bypass", ["--dangerously-skip-permissions"]),
    ],
}

MINIMAX_MODELS = [
    ModelItem("default", "Default"),
    ModelItem("MiniMax-M3", "MiniMax-M3", True, [{"type": "toggle"}]),
    ModelItem("MiniMax-M2.7", "MiniMax-M2.7", True, []),
    ModelItem("MiniMax-M2.7-highspeed", "MiniMax-M2.7 High Speed", True, []),
    ModelItem("MiniMax-M2.5", "MiniMax-M2.5", True, []),
    ModelItem("MiniMax-M2.5-highspeed", "MiniMax-M2.5 High Speed", True, []),
    ModelItem("MiniMax-M2", "MiniMax-M2", True, []),
]

OPENCODE_GO_MODELS = [
    ModelItem("default", "Default"),
    ModelItem("minimax-m3", "MiniMax M3", True, [{"type": "toggle"}]),
    ModelItem("deepseek-v4-pro", "DeepSeek V4 Pro", True, [{"type": "effort", "values": ["high", "max"]}]),
    ModelItem("deepseek-v4-flash", "DeepSeek V4 Flash", True, [{"type": "effort", "values": ["high", "max"]}]),
    ModelItem("qwen3.7-max", "Qwen 3.7 Max", True, []),
    ModelItem("qwen3.7-plus", "Qwen 3.7 Plus", True, []),
    ModelItem("qwen3.5-plus", "Qwen 3.5 Plus", True, []),
    ModelItem("kimi-k2.7-code", "Kimi K2.7 Code", True, []),
    ModelItem("kimi-k2.6", "Kimi K2.6", True, []),
    ModelItem("mimo-v2.5-pro", "Mimo V2.5 Pro", True, []),
    ModelItem("mimo-v2-omni", "Mimo V2 Omni", True, []),
    ModelItem("glm-5.1", "GLM 5.1", True, []),
    ModelItem("glm-5", "GLM 5", True, []),
]


def load_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        data[key.strip()] = value
    return data


def save_env_values(path: Path, updates: dict[str, str]) -> None:
    existing = load_env_file(path)
    existing.update(updates)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f'{key}="{value}"' for key, value in sorted(existing.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_json_file(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def load_opencode_auth() -> dict:
    data = load_json_file(OPENCODE_AUTH_FILE)
    if isinstance(data, dict):
        return data
    return {}


def get_openrouter_api_key() -> str | None:
    for path in [OPENROUTER_SHARED_ENV_FILE, OPENROUTER_PROFILE_ENV_FILE]:
        key = load_env_file(path).get("OPENROUTER_API_KEY")
        if key:
            return key
    auth = load_opencode_auth()
    return auth.get("openrouter", {}).get("key")


def get_minimax_api_key() -> str | None:
    key = load_env_file(MINIMAX_ENV_FILE).get("MINIMAX_API_KEY")
    if key:
        return key
    key = load_env_file(HARNESS_ENV_FILE).get("MINIMAX_API_KEY")
    if key:
        return key
    auth = load_opencode_auth()
    return auth.get("minimax", {}).get("key")


def get_provider_status(provider: ProviderDefinition) -> ProviderStatus:
    if provider.provider_id == "claude":
        import subprocess as sp
        result = sp.run(["claude", "auth", "status"], capture_output=True, text=True)
        logged_in = False
        try:
            logged_in = bool(json.loads(result.stdout.strip()).get("loggedIn"))
        except Exception:
            pass
        return ProviderStatus(logged_in, "login ok" if logged_in else "falta login")
    if provider.provider_id == "openrouter":
        has_key = bool(get_openrouter_api_key())
        return ProviderStatus(has_key, "login ok" if has_key else "falta login")
    if provider.provider_id == "opencode-go":
        auth = load_opencode_auth()
        has_auth = provider.provider_id in auth
        has_env = bool(load_env_file(HARNESS_ENV_FILE).get("OPENCODE_GO_API_KEY"))
        return ProviderStatus(has_auth or has_env, "login ok" if (has_auth or has_env) else "falta login")
    if provider.provider_id == "minimax":
        has_key = bool(get_minimax_api_key())
        return ProviderStatus(has_key, "login ok" if has_key else "falta login")
    if provider.provider_id == "codex":
        import shutil
        import subprocess as sp
        if not shutil.which("codex"):
            return ProviderStatus(False, "codex CLI no instalado")
        result = sp.run(["codex", "login", "status"], capture_output=True, text=True)
        output = " ".join(p.strip() for p in [result.stdout, result.stderr] if p.strip()).lower()
        if "logged in" not in output:
            return ProviderStatus(False, "login: device-auth")
        proxy_url = os.environ.get("CLAUDE_HARNESS_CODEX_PROXY_URL", "").strip()
        if not proxy_url:
            proxy_url = load_env_file(HARNESS_ENV_FILE).get("CLAUDE_HARNESS_CODEX_PROXY_URL", "").strip()
        if not proxy_url:
            return ProviderStatus(False, "falta proxy URL (Ctrl+L)")
        return ProviderStatus(True, "ok")
    return ProviderStatus(False, "desconocido")


def get_default_model(provider: ProviderDefinition) -> str | None:
    if not provider.default_model_env:
        return None
    return load_env_file(HARNESS_ENV_FILE).get(provider.default_model_env)


def set_default_model(provider: ProviderDefinition, model_id: str) -> None:
    if not provider.default_model_env:
        return
    save_env_values(HARNESS_ENV_FILE, {provider.default_model_env: model_id})


def fetch_claude_models() -> list[ModelItem]:
    items = [ModelItem("default", "Default"), ModelItem("opus", "Opus"),
             ModelItem("sonnet", "Sonnet"), ModelItem("haiku", "Haiku"),
             ModelItem("fable", "Fable")]
    state = load_json_file(CLAUDE_STATE_FILE)
    if not isinstance(state, dict):
        return items
    additional = state.get("additionalModelOptionsCache", [])
    if isinstance(additional, list):
        for entry in additional:
            if not isinstance(entry, dict):
                continue
            slug = str(entry.get("value", "")).split("[", 1)[0].strip()
            label = str(entry.get("label", slug or "Modelo"))
            if slug:
                items.append(ModelItem(slug, label))
    return items


def fetch_codex_models() -> list[ModelItem]:
    items = [ModelItem("default", "Default")]
    raw = load_json_file(CODEX_MODELS_FILE)
    models_raw = []
    if isinstance(raw, dict):
        models_raw = raw.get("models", [])
    elif isinstance(raw, list):
        models_raw = raw
    for entry in models_raw:
        if not isinstance(entry, dict):
            continue
        if entry.get("visibility") == "hide":
            continue
        slug = str(entry.get("slug", "")).strip()
        if not slug:
            continue
        label = str(entry.get("display_name", slug)).strip() or slug
        items.append(ModelItem(slug, label))
    if len(items) == 1:
        items.extend([
            ModelItem("gpt-5.5", "GPT-5.5"),
            ModelItem("gpt-5.4", "GPT-5.4"),
            ModelItem("gpt-5.4-mini", "GPT-5.4-Mini"),
            ModelItem("gpt-5.3-codex-spark", "GPT-5.3 Codex Spark"),
        ])
    return items


def fetch_openrouter_models() -> list[ModelItem]:
    api_key = get_openrouter_api_key()
    if not api_key:
        raise RuntimeError("OpenRouter no tiene API key configurada. Usa Ctrl+L en el menu de proveedores.")
    request = urllib.request.Request(
        "https://openrouter.ai/api/v1/models",
        headers={"Authorization": f"Bearer {api_key}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(body or f"OpenRouter devolvio HTTP {error.code}")
    except Exception as error:
        raise RuntimeError(str(error))
    items = [ModelItem("default", "Default")]
    for entry in payload.get("data", []):
        if not isinstance(entry, dict):
            continue
        slug = str(entry.get("id", "")).strip()
        if not slug:
            continue
        label = str(entry.get("name", slug)).strip() or slug
        items.append(ModelItem(slug, label))
    return items


def load_model_cache() -> dict:
    raw = load_json_file(HARNESS_CACHE_FILE)
    if isinstance(raw, dict):
        return raw
    return {}


def save_model_cache(cache: dict) -> None:
    HARNESS_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    HARNESS_CACHE_FILE.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_favorites() -> dict[str, list[str]]:
    raw = load_json_file(HARNESS_FAVORITES_FILE)
    if isinstance(raw, dict):
        return {k: v for k, v in raw.items() if isinstance(v, list)}
    return {}


def save_favorites(data: dict[str, list[str]]) -> None:
    HARNESS_FAVORITES_FILE.parent.mkdir(parents=True, exist_ok=True)
    HARNESS_FAVORITES_FILE.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def fetch_models_dev_catalog(force: bool = False) -> dict[str, dict[str, dict]] | None:
    if not force and MODELS_DEV_CACHE.exists():
        try:
            mtime = MODELS_DEV_CACHE.stat().st_mtime
            if time.time() - mtime < MODELS_DEV_TTL:
                data = load_json_file(MODELS_DEV_CACHE)
                if isinstance(data, dict) and data:
                    return _extract_caps(data)
        except Exception:
            pass

    try:
        request = urllib.request.Request(
            MODELS_DEV_URL,
            headers={"User-Agent": "claude-harness/1.0", "Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if isinstance(data, dict) and data:
            try:
                MODELS_DEV_CACHE.parent.mkdir(parents=True, exist_ok=True)
                MODELS_DEV_CACHE.write_text(
                    json.dumps(data, indent=0, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            except Exception:
                pass
            return _extract_caps(data)
    except Exception:
        pass

    if MODELS_DEV_CACHE.exists():
        try:
            data = load_json_file(MODELS_DEV_CACHE)
            if isinstance(data, dict) and data:
                return _extract_caps(data)
        except Exception:
            pass

    return None


def _extract_caps(data: dict) -> dict[str, dict[str, dict]]:
    caps: dict[str, dict[str, dict]] = {}
    for provider_id, provider_data in data.items():
        if not isinstance(provider_data, dict):
            continue
        models = provider_data.get("models", {})
        if isinstance(models, dict):
            caps[provider_id] = models
    return caps


# Mapeo de provider_id del harness -> id en models.dev (algunos difieren).
PROVIDER_TO_CATALOG_ID = {
    "minimax": "minimax",
    "openrouter": "openrouter",
    "opencode-go": "opencode",
    "codex": "openai",
    "claude": "anthropic",
}


def _lookup_model_info(model_id: str, catalog: dict[str, dict[str, dict]] | None,
                       provider_id: str | None = None) -> dict | None:
    if not catalog:
        return None
    if provider_id:
        cat_id = PROVIDER_TO_CATALOG_ID.get(provider_id, provider_id)
        prov_models = catalog.get(cat_id)
        if isinstance(prov_models, dict):
            for cand in (model_id, model_id.split("/")[-1], model_id.lower(), model_id.split("/")[-1].lower()):
                if cand in prov_models:
                    return prov_models[cand]
    needle = model_id.split("/")[-1].lower()
    for prov_models in catalog.values():
        if not isinstance(prov_models, dict):
            continue
        for mid, info in prov_models.items():
            if isinstance(mid, str) and mid.lower() == needle:
                return info
    return None


def get_model_context_window(model_id: str, catalog: dict[str, dict[str, dict]] | None,
                             provider_id: str | None = None) -> int:
    info = _lookup_model_info(model_id, catalog, provider_id)
    if info and isinstance(info, dict):
        ctx = info.get("limit", {}).get("context")
        if isinstance(ctx, (int, float)) and ctx > 0:
            return int(ctx)
    if model_id and "[1m]" in model_id.lower():
        return 1_000_000
    return 200_000


def _should_auto_set_context_window() -> bool:
    val = os.environ.get("CLAUDE_HARNESS_CONTEXT_WINDOW", "1").strip().lower()
    return val not in ("0", "false", "no", "off")


CLAUDE_CODE_KNOWN_CONTEXT_FOR_UNLISTED = 200_000
ANTHROPIC_PROVIDER_IDS = {"claude"}


def is_anthropic_model(model_id: str | None, provider_id: str | None) -> bool:
    if provider_id in ANTHROPIC_PROVIDER_IDS:
        return True
    if provider_id == "openrouter" and model_id:
        ml = model_id.lower()
        return ml.startswith("anthropic/") or "/claude-" in ml
    return False


# Anthropic 1M-context model names that Claude Code recognizes with [1m].
# Source: strings dump of Claude Code 2.1.178 binary, plus models.dev.
# DO NOT add claude-sonnet-4-5 or claude-haiku-4-5 here: they are 200k.
ANTHROPIC_1M_MODEL_NAMES = frozenset({
    "claude-fable-5",
    "claude-mythos-5",
    "claude-opus-4-6",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-sonnet-4-6",
})


def model_id_for_claude_code(model_id: str, provider_id: str | None,
                             catalog: dict[str, dict[str, dict]] | None = None) -> str:
    """Agrega [1m] suffix SOLO para modelos que Claude Code lo reconoce.

    Claude Code reconoce el truco [1m] unicamente para sus propios modelos
    con 1M context (claude-opus-4-6[1m], claude-sonnet-4-6[1m], etc.).
    Para modelos OpenAI (gpt-5.4-mini, gpt-5.5, etc.) el truco NO funciona:
    Claude Code hace strip del [1m], busca el modelo en su catalogo
    interno, no lo encuentra, y muestra el default de 200k. Agregar [1m]
    ahi solo confunde y, peor, hace que el auto-compact threshold se
    compute mal (1M * 0.9 = 900k en vez de 400k * 0.9 = 360k).

    Para modelos no-Anthropic usamos CLAUDE_CODE_AUTO_COMPACT_WINDOW
    (configurado en apply_context_window_env) para que el auto-compact
    respete el context real. El display caera a 200k (limitacion de
    Claude Code para modelos fuera de su catalogo), pero la logica de
    compact esta bien seteada.
    """
    if not model_id or model_id == "default":
        return model_id
    ml = model_id.lower()
    if "[1m]" in ml or "[2m]" in ml:
        return model_id
    # Solo Anthropic: el [1m] funciona en Claude Code
    if is_anthropic_model(model_id, provider_id):
        # 1) Si tenemos catalog, verificar context real
        info = _lookup_model_info(model_id, catalog, provider_id)
        if info and isinstance(info, dict):
            ctx = info.get("limit", {}).get("context")
            if isinstance(ctx, (int, float)) and ctx >= 1_000_000:
                return f"{model_id}[1m]"
            return model_id
        # 2) Sin catalog: usar lista hardcoded de modelos Anthropic 1M
        #    que Claude Code reconoce con [1m]
        bare = model_id.split("/")[-1].lower()
        if bare in ANTHROPIC_1M_MODEL_NAMES:
            return f"{model_id}[1m]"
        return model_id
    # No-Anthropic: NO agregar [1m]. Claude Code no lo reconocera y
    # el auto-compact ya se configura via CLAUDE_CODE_AUTO_COMPACT_WINDOW.
    return model_id


def _claude_code_known_context(model_id: str) -> int:
    ml = model_id.lower() if model_id else ""
    if "[1m]" in ml:
        return 1_000_000
    if any(name in ml for name in (
        "claude-opus-4-1", "claude-opus-4-0", "claude-4-opus",
        "claude-sonnet-4-0", "claude-sonnet-4-1", "claude-4-sonnet",
        "claude-3-5-sonnet", "claude-3-5-haiku", "claude-3-7-sonnet",
    )):
        return 200_000
    if "claude-3-haiku" in ml:
        return 200_000
    if "claude-2" in ml or "claude-instant" in ml:
        return 100_000
    return CLAUDE_CODE_KNOWN_CONTEXT_FOR_UNLISTED


def apply_context_window_env(model_id: str, catalog: dict[str, dict[str, dict]] | None,
                             provider_id: str | None = None) -> dict | None:
    if not _should_auto_set_context_window():
        return None
    real_ctx = get_model_context_window(model_id, catalog, provider_id)
    if real_ctx <= 0:
        return None
    is_anthropic = is_anthropic_model(model_id, provider_id)
    if is_anthropic:
        known_ctx = _claude_code_known_context(model_id)
        threshold = int(known_ctx * 0.9)
        if "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in os.environ:
            os.environ["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(threshold)
        os.environ.pop("DISABLE_AUTO_COMPACT", None)
        return {
            "real_ctx": real_ctx,
            "known_ctx": known_ctx,
            "threshold": threshold,
            "auto_compact": True,
        }
    cc_model = model_id_for_claude_code(model_id, provider_id, catalog)
    known_ctx = _claude_code_known_context(cc_model)
    threshold = int(real_ctx * 0.9)
    if "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in os.environ:
        os.environ["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(threshold)
    os.environ.pop("DISABLE_AUTO_COMPACT", None)
    return {
        "real_ctx": real_ctx,
        "known_ctx": known_ctx,
        "threshold": threshold,
        "auto_compact": True,
    }


def load_opencode_model_capabilities() -> dict[str, dict[str, dict]]:
    catalog = fetch_models_dev_catalog()
    if catalog:
        return catalog
    data = load_json_file(OPENCODE_MODELS_CACHE)
    if isinstance(data, dict):
        return _extract_caps(data)
    return {}


def enrich_models_with_reasoning(provider_id: str, models: list[ModelItem]) -> list[ModelItem]:
    caps = load_opencode_model_capabilities()
    pid_map = {"minimax": "minimax", "opencode-go": "opencode-go", "openrouter": "openrouter"}
    mapped = pid_map.get(provider_id, "")
    provider_models = caps.get(mapped, {}) if mapped else {}
    for m in models:
        if m.reasoning_options is not None:
            continue
        md = provider_models.get(m.model_id)
        if isinstance(md, dict):
            m.reasoning = md.get("reasoning", False)
            m.reasoning_options = md.get("reasoning_options", [])
        else:
            m.reasoning_options = []
    return models


def fetch_models_for_provider(provider: ProviderDefinition, force_refresh: bool = False) -> list[ModelItem]:
    cache = load_model_cache()
    cache_key = provider.provider_id
    cached_entry = cache.get(cache_key, {})
    cached_models = cached_entry.get("models", [])
    cached_at = float(cached_entry.get("fetched_at", 0))
    if not force_refresh and cached_models and time.time() - cached_at < MODEL_CACHE_TTL_SECONDS:
        return [ModelItem(i["model_id"], i["label"],
                          i.get("reasoning", False),
                          i.get("reasoning_options"))
                for i in cached_models if isinstance(i, dict)]
    if provider.provider_id == "claude":
        models = fetch_claude_models()
    elif provider.provider_id == "codex":
        models = fetch_codex_models()
    elif provider.provider_id == "openrouter":
        models = fetch_openrouter_models()
    elif provider.provider_id == "minimax":
        models = MINIMAX_MODELS
    elif provider.provider_id == "opencode-go":
        models = OPENCODE_GO_MODELS
    else:
        models = [ModelItem("default", "Default")]
    models = enrich_models_with_reasoning(provider.provider_id, models)
    cache[cache_key] = {
        "fetched_at": time.time(),
        "models": [{"model_id": m.model_id, "label": m.label,
                    "reasoning": m.reasoning, "reasoning_options": m.reasoning_options}
                   for m in models],
    }
    save_model_cache(cache)
    return models


def find_provider(provider_id: str) -> ProviderDefinition | None:
    for p in PROVIDERS:
        if p.provider_id == provider_id:
            return p
    return None


def map_effort_to_model(claude_level: str, model_effort_values: list[str]) -> str:
    if claude_level in model_effort_values:
        return claude_level
    if claude_level not in CLAUDE_EFFORT_LEVELS:
        return model_effort_values[-1] if model_effort_values else claude_level
    claude_idx = CLAUDE_EFFORT_LEVELS.index(claude_level)
    best = model_effort_values[0]
    best_dist = float("inf")
    for mv in model_effort_values:
        if mv in CLAUDE_EFFORT_LEVELS:
            dist = abs(CLAUDE_EFFORT_LEVELS.index(mv) - claude_idx)
            if dist < best_dist:
                best_dist = dist
                best = mv
            elif dist == best_dist and CLAUDE_EFFORT_LEVELS.index(mv) > CLAUDE_EFFORT_LEVELS.index(best):
                best = mv
    return best


@dataclass
class ThinkingCaps:
    mode: str
    effort_levels: list[str]
    supports_xhigh: bool
    forbids_sampling_params: bool
    toggle_budget: int = TOGGLE_DEFAULT_BUDGET
    is_legacy: bool = False
    is_adaptive: bool = False

    @property
    def label(self) -> str:
        if self.mode == "disabled":
            return "sin reasoning"
        if self.mode == "toggle":
            return f"toggle (on/off, budget={self.toggle_budget})"
        if self.mode == "legacy":
            return "legacy (budget_tokens)"
        if self.mode == "adaptive_4_6":
            return f"adaptive 4.6 (effort: {','.join(self.effort_levels)})"
        if self.mode == "adaptive_4_7":
            return f"adaptive 4.7+ (effort: {','.join(self.effort_levels)})"
        return self.mode


def detect_thinking_capabilities(model_id: str, reasoning_options: list[dict] | None = None) -> ThinkingCaps:
    mid = (model_id or "").lower()

    if "minimax" in mid:
        if "m3" in mid or "minimax-m3" in mid:
            return ThinkingCaps("toggle", [], False, False, toggle_budget=TOGGLE_DEFAULT_BUDGET)
        return ThinkingCaps("toggle", [], False, False, toggle_budget=TOGGLE_DEFAULT_BUDGET)

    import re
    is_anthropic = "claude" in mid or "anthropic" in mid
    is_4_7_plus = bool(re.search(r'(?:opus|sonnet|claude)[-.]?4[.-][78]', mid))
    is_4_6 = bool(re.search(r'(?:opus|sonnet|claude)[-.]?4[.-]6(?![\d.])', mid))
    is_4_5_or_earlier = bool(re.search(r'claude[-.]?(3[.-]?[7-9]|4[.-]?[0-5])', mid)) if is_anthropic else False

    if is_anthropic and is_4_7_plus:
        return ThinkingCaps("adaptive_4_7",
                            ["low", "medium", "high", "xhigh", "max"],
                            True, True)
    if is_anthropic and is_4_6:
        return ThinkingCaps("adaptive_4_6",
                            ["low", "medium", "high", "max"],
                            False, False)
    if is_anthropic and is_4_5_or_earlier:
        return ThinkingCaps("legacy", [], False, False, is_legacy=True)

    if reasoning_options:
        effort_opt = next((o for o in reasoning_options if o.get("type") == "effort"), None)
        if effort_opt and effort_opt.get("values"):
            values = [v for v in effort_opt["values"] if v in CLAUDE_EFFORT_LEVELS]
            if "xhigh" in values:
                return ThinkingCaps("adaptive_4_7", values, True, True)
            return ThinkingCaps("adaptive_4_6", values, False, False)
        if any(o.get("type") == "toggle" for o in reasoning_options):
            return ThinkingCaps("toggle", [], False, False, toggle_budget=TOGGLE_DEFAULT_BUDGET)
        if any(o.get("type") == "budget_tokens" for o in reasoning_options):
            return ThinkingCaps("legacy", [], False, False, is_legacy=True)
        return ThinkingCaps("legacy", [], False, False, is_legacy=True)

    return ThinkingCaps("legacy", [], False, False, is_legacy=True)


def build_thinking_params(level: str, caps: ThinkingCaps) -> dict:
    if level == "off" or level is None:
        return {}
    if caps.mode == "disabled":
        return {}

    if caps.mode == "toggle":
        return {
            "thinking": {"type": "enabled", "budget_tokens": caps.toggle_budget}
        }

    if caps.mode == "legacy":
        budget = THINKING_BUDGET_TOKENS.get(level, THINKING_BUDGET_TOKENS["medium"])
        return {
            "thinking": {"type": "enabled", "budget_tokens": budget}
        }

    if caps.mode == "adaptive_4_7":
        effort = ADAPTIVE_EFFORT_MAP.get(level, "medium")
        return {
            "thinking": {"type": "adaptive", "display": "summarized"},
            "output_config": {"effort": effort}
        }

    if caps.mode == "adaptive_4_6":
        effort = ADAPTIVE_NO_XHIGH_MAP.get(level, "medium")
        return {
            "thinking": {"type": "adaptive", "display": "summarized"},
            "output_config": {"effort": effort}
        }

    return {}


def format_thinking_params(params: dict) -> str:
    if not params:
        return "off (no thinking)"
    if "output_config" in params:
        effort = params.get("output_config", {}).get("effort", "?")
        return f"adaptive / effort={effort}"
    thinking = params.get("thinking", {})
    if thinking.get("type") == "enabled":
        budget = thinking.get("budget_tokens", "?")
        return f"legacy / budget_tokens={budget}"
    if thinking.get("type") == "adaptive":
        return f"adaptive (sin output_config)"
    return f"thinking: {thinking}"


def get_thinking_options(model_id: str, reasoning: bool, reasoning_options: list[dict] | None) -> list[tuple[str, str]]:
    caps = detect_thinking_capabilities(model_id, reasoning_options)
    if caps.mode == "disabled":
        return [("off", "Off (modelo sin reasoning)")]
    results: list[tuple[str, str]] = []
    if caps.mode == "toggle":
        for cl in ["minimal", "low", "medium", "high", "xhigh", "ultracode"]:
            params = build_thinking_params(cl, caps)
            results.append((cl, f"{cl} -> {format_thinking_params(params)}"))
        results.append(("off", "Off (sin thinking)"))
        return results
    if caps.mode in ("adaptive_4_6", "adaptive_4_7"):
        for cl in ["minimal", "low", "medium", "high", "xhigh", "max", "ultracode"]:
            params = build_thinking_params(cl, caps)
            results.append((cl, f"{cl} -> {format_thinking_params(params)}"))
        results.append(("off", "Off (sin thinking)"))
        return results
    if caps.mode == "legacy":
        for cl in ["minimal", "low", "medium", "high", "xhigh", "max", "ultracode"]:
            params = build_thinking_params(cl, caps)
            results.append((cl, f"{cl} -> {format_thinking_params(params)}"))
        results.append(("off", "Off (sin thinking)"))
        return results
    return [("off", "Off")]


import curses
import curses.ascii
import locale
import subprocess

PAGE_SIZE = 10
KEY_CTRL_C = chr(3)
KEY_CTRL_D = chr(4)
KEY_CTRL_F = chr(6)
KEY_CTRL_L = chr(12)
KEY_CTRL_Q = chr(17)
KEY_CTRL_R = chr(18)
KEY_CTRL_S = chr(19)
KEY_CTRL_X = chr(24)
KEY_ESC = chr(27)

HAS_COLORS = False
CP_TITLE = 0
CP_SELECT = 0
CP_DANGER = 0
CP_OK = 0
CP_WARN = 0
CP_DIM = 0
CP_STAR = 0
CP_HINT = 0
CP_HEADER = 0


def init_colors() -> None:
    global HAS_COLORS, CP_TITLE, CP_SELECT, CP_DANGER, CP_OK, CP_WARN
    global CP_DIM, CP_STAR, CP_HINT, CP_HEADER
    HAS_COLORS = curses.has_colors() and curses.can_change_color()
    if not HAS_COLORS:
        return
    try:
        curses.start_color()
        curses.use_default_colors()
        CP_TITLE = 1
        curses.init_pair(CP_TITLE, curses.COLOR_CYAN, -1)
        CP_SELECT = 2
        curses.init_pair(CP_SELECT, curses.COLOR_BLACK, curses.COLOR_CYAN)
        CP_DANGER = 3
        curses.init_pair(CP_DANGER, curses.COLOR_RED, -1)
        CP_OK = 4
        curses.init_pair(CP_OK, curses.COLOR_GREEN, -1)
        CP_WARN = 5
        curses.init_pair(CP_WARN, curses.COLOR_YELLOW, -1)
        CP_DIM = 6
        curses.init_pair(CP_DIM, curses.COLOR_WHITE, -1)
        CP_STAR = 7
        curses.init_pair(CP_STAR, curses.COLOR_YELLOW, -1)
        CP_HINT = 8
        curses.init_pair(CP_HINT, curses.COLOR_CYAN, -1)
        CP_HEADER = 9
        curses.init_pair(CP_HEADER, curses.COLOR_MAGENTA, -1)
    except Exception:
        HAS_COLORS = False


def attr_pair(cp: int) -> int:
    if HAS_COLORS and cp > 0:
        return curses.color_pair(cp)
    return curses.A_NORMAL


def attr_bold() -> int:
    try:
        return curses.A_BOLD
    except Exception:
        return curses.A_NORMAL


def draw_header(stdscr, title: str, subtitle: str = "") -> None:
    h, w = stdscr.getmaxyx()
    stdscr.move(0, 0)
    stdscr.clrtoeol()
    try:
        stdscr.addnstr(0, 0, f" {title} ", w - 1, attr_pair(CP_HEADER) | attr_bold())
    except Exception:
        stdscr.addnstr(0, 0, f" {title} ", w - 1)
    if subtitle:
        try:
            stdscr.addnstr(0, len(title) + 3, f" {subtitle} ", w - 1, attr_pair(CP_DIM))
        except Exception:
            pass
    stdscr.move(1, 0)
    stdscr.clrtoeol()
    if h > 2:
        try:
            stdscr.addnstr(1, 0, "-" * (w - 1), w - 1, attr_pair(CP_DIM))
        except Exception:
            try:
                stdscr.addnstr(1, 0, "-" * (w - 1), w - 1, attr_pair(CP_DIM))
            except Exception:
                pass


def draw_footer(stdscr, text: str) -> None:
    h, w = stdscr.getmaxyx()
    stdscr.move(h - 1, 0)
    stdscr.clrtoeol()
    try:
        stdscr.addnstr(h - 1, 0, " " + text, w - 1, attr_pair(CP_DIM))
    except Exception:
        pass


def safe_addstr(stdscr, y: int, x: int, text: str, attr: int = 0) -> None:
    try:
        if attr:
            stdscr.addnstr(y, x, text, stdscr.getmaxyx()[1] - x - 1, attr)
        else:
            stdscr.addnstr(y, x, text, stdscr.getmaxyx()[1] - x - 1)
    except curses.error:
        pass


def show_message(stdscr, lines: list[str], wait_key: bool = True) -> None:
    h, w = stdscr.getmaxyx()
    stdscr.erase()
    safe_addstr(stdscr, 0, 0, " ".join(lines), attr_pair(CP_TITLE) | attr_bold())
    if wait_key:
        draw_footer(stdscr, "Presiona Enter o Esc para volver...")
        stdscr.refresh()
        stdscr.nodelay(False)
        stdscr.getch()


def run_external_in_curses(stdscr, args: list[str], stdin_text: str | None = None) -> None:
    curses.endwin()
    print()
    print(" ".join(args))
    print()
    try:
        subprocess.run(args, input=stdin_text, text=True, check=False)
    finally:
        print()
        try:
            input("Presiona Enter para volver al harness...")
        except EOFError:
            pass
    stdscr.refresh()


def draw_provider_list(stdscr, providers, statuses, index: int) -> None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    draw_header(stdscr, "Claude Harness", "Selecciona un proveedor")
    for i, p in enumerate(providers):
        y = 3 + i
        if y >= h - 2:
            break
        s = statuses[i]
        is_sel = (i == index)
        marker = "> " if is_sel else "  "
        tags = []
        if p.experimental:
            tags.append(("exp", CP_WARN))
        if not p.supports_claude_launch:
            tags.append(("config", CP_DIM))
        tag_str = ""
        for t, cp in tags:
            tag_str += f" [{t}]"
        status_text = s.detail
        status_cp = CP_OK if s.logged_in else CP_DANGER
        line = f"{marker}{i+1}. {p.label}{tag_str}"
        attr = attr_pair(CP_SELECT) | attr_bold() if is_sel else curses.A_NORMAL
        safe_addstr(stdscr, y, 0, line, attr)
        line_w = len(line)
        # Status right-aligned, but allow up to 35 chars (was 20).
        if line_w < w - 1:
            status_x = max(line_w + 2, w - len(status_text) - 4)
            safe_addstr(stdscr, y, status_x, f"[{status_text}]", attr_pair(status_cp))
    draw_footer(stdscr, "[Enter] elegir  [Ctrl+L] login  [Ctrl+R] refresh  [Esc] salir")


def draw_login_menu(stdscr, provider, index: int) -> None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    draw_header(stdscr, f"Login: {provider.label}")
    items = login_actions_for(provider.provider_id)
    for i, (label, _) in enumerate(items):
        y = 3 + i
        if y >= h - 2:
            break
        is_sel = (i == index)
        marker = "> " if is_sel else "  "
        line = f"{marker}{i+1}. {label}"
        attr = attr_pair(CP_SELECT) | attr_bold() if is_sel else curses.A_NORMAL
        safe_addstr(stdscr, y, 0, line, attr)
    draw_footer(stdscr, "[Enter] ejecutar  [Esc] volver")


def login_actions_for(pid: str):
    if pid == "claude":
        return [("Claude subscription (claude.ai)", lambda: ["claude", "auth", "login", "--claudeai"]),
                ("Anthropic Console (API key)", lambda: ["claude", "auth", "login", "--console"])]
    if pid == "openrouter":
        return [("Login via OpenCode", lambda: ["opencode", "providers", "login", "-p", "openrouter"]),
                ("API key manual", None)]
    if pid == "opencode-go":
        return [("Login via OpenCode", lambda: ["opencode", "providers", "login", "-p", "opencode-go"]),
                ("API key manual", None)]
    if pid == "minimax":
        return [("Login via OpenCode", lambda: ["opencode", "providers", "login", "-p", "minimax"]),
                ("API key manual", None)]
    if pid == "codex":
        return [("Device auth (link device)", lambda: ["codex", "login", "--device-auth"]),
                ("Set proxy URL", "codex-proxy-url"),
                ("API key manual", None)]
    return []


def do_login_action(stdscr, provider, idx: int) -> None:
    items = login_actions_for(provider.provider_id)
    if idx >= len(items):
        return
    label, action = items[idx]
    if action is None:
        prompt_api_key(stdscr, provider.provider_id)
        return
    if isinstance(action, str):
        if action == "codex-proxy-url":
            prompt_codex_proxy_url(stdscr)
            return
    # For Codex, after device-auth finishes, if the proxy URL is not set,
    # automatically prompt for it so the user doesn't have to re-enter the menu.
    if provider.provider_id == "codex":
        run_external_in_curses(stdscr, action())
        status = get_provider_status(provider)
        if status.logged_in:
            proxy_url = os.environ.get("CLAUDE_HARNESS_CODEX_PROXY_URL", "").strip()
            if not proxy_url:
                proxy_url = load_env_file(HARNESS_ENV_FILE).get("CLAUDE_HARNESS_CODEX_PROXY_URL", "").strip()
            if not proxy_url:
                prompt_codex_proxy_url(stdscr)
        return
    run_external_in_curses(stdscr, action())


def prompt_api_key(stdscr, pid: str) -> None:
    curses.endwin()
    print()
    import getpass
    if pid == "openrouter":
        key = getpass.getpass("  OPENROUTER_API_KEY: ").strip()
        if key:
            cur = load_env_file(OPENROUTER_SHARED_ENV_FILE)
            img = cur.get("OPENROUTER_IMAGE_MODEL", "x-ai/grok-imagine-image-quality")
            save_env_values(OPENROUTER_SHARED_ENV_FILE, {"OPENROUTER_API_KEY": key, "OPENROUTER_IMAGE_MODEL": img})
            print("  Guardada.")
    elif pid == "opencode-go":
        key = getpass.getpass("  OPENCODE_GO_API_KEY: ").strip()
        if key:
            save_env_values(HARNESS_ENV_FILE, {"OPENCODE_GO_API_KEY": key})
            print("  Guardada.")
    elif pid == "minimax":
        key = getpass.getpass("  MINIMAX_API_KEY: ").strip()
        if key:
            cur = load_env_file(MINIMAX_ENV_FILE)
            updates = {"MINIMAX_API_KEY": key}
            if "MINIMAX_BASE_URL" in cur:
                updates["MINIMAX_BASE_URL"] = cur["MINIMAX_BASE_URL"]
            save_env_values(MINIMAX_ENV_FILE, updates)
            print("  Guardada.")
    elif pid == "codex":
        key = getpass.getpass("  OPENAI_API_KEY: ").strip()
        if key:
            subprocess.run(["codex", "login", "--with-api-key"], input=key + "\n", text=True)
    print()
    try:
        input("  Enter para volver al harness...")
    except EOFError:
        pass
    stdscr.refresh()


def prompt_codex_proxy_url(stdscr) -> None:
    curses.endwin()
    print()
    current = load_env_file(HARNESS_ENV_FILE).get("CLAUDE_HARNESS_CODEX_PROXY_URL", "").strip()
    if current:
        print(f"  Actual: {current}")
    try:
        url = input("  CLAUDE_HARNESS_CODEX_PROXY_URL (https://...workers.dev): ").strip()
    except EOFError:
        url = ""
    if url:
        save_env_values(HARNESS_ENV_FILE, {"CLAUDE_HARNESS_CODEX_PROXY_URL": url})
        print("  Guardada.")
    print()
    try:
        input("  Enter para volver al harness...")
    except EOFError:
        pass
    stdscr.refresh()


def pick_provider(stdscr) -> ProviderDefinition | None:
    statuses = [get_provider_status(p) for p in PROVIDERS]
    index = 0
    while True:
        draw_provider_list(stdscr, PROVIDERS, statuses, index)
        stdscr.refresh()
        key = stdscr.getch()
        if key == -1:
            continue
        if key in (27, ord("q"), ord("Q")):
            return None
        if key == curses.KEY_UP:
            index = (index - 1) % len(PROVIDERS)
        elif key == curses.KEY_DOWN:
            index = (index + 1) % len(PROVIDERS)
        elif key == curses.KEY_HOME or key == curses.KEY_PPAGE:
            index = 0
        elif key == curses.KEY_END or key == curses.KEY_NPAGE:
            index = len(PROVIDERS) - 1
        elif key in (curses.KEY_ENTER, 10, 13):
            p = PROVIDERS[index]
            if not statuses[index].logged_in:
                if run_login_menu(stdscr, p):
                    statuses = [get_provider_status(pp) for pp in PROVIDERS]
                continue
            return p
        elif key == KEY_CTRL_L or key == ord("l") or key == ord("L"):
            if run_login_menu(stdscr, PROVIDERS[index]):
                statuses = [get_provider_status(pp) for pp in PROVIDERS]
        elif key == KEY_CTRL_R or key == ord("r") or key == ord("R"):
            statuses = [get_provider_status(pp) for pp in PROVIDERS]


def run_login_menu(stdscr, provider) -> bool:
    items = login_actions_for(provider.provider_id)
    if not items:
        return False
    index = 0
    while True:
        draw_login_menu(stdscr, provider, index)
        stdscr.refresh()
        key = stdscr.getch()
        if key == -1:
            continue
        if key == 27:
            return False
        if key == curses.KEY_UP:
            index = (index - 1) % len(items)
        elif key == curses.KEY_DOWN:
            index = (index + 1) % len(items)
        elif key in (curses.KEY_ENTER, 10, 13):
            do_login_action(stdscr, provider, index)
            return True


def draw_model_list(stdscr, provider, models, filtered, favs, default_model,
                    query: str, show_favs_only: bool, scroll: int, sel: int,
                    error: str = "") -> None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    subtitle = f"{provider.label}  |  {len(filtered)} modelos"
    if show_favs_only:
        subtitle += "  |  solo favoritos"
    if query:
        subtitle += f"  |  buscar: {query}"
    draw_header(stdscr, "Modelos", subtitle)

    row = 3
    if error:
        safe_addstr(stdscr, row, 0, f"! {error}", attr_pair(CP_DANGER))
        row += 1
    if not filtered:
        msg = "no hay modelos"
        if show_favs_only:
            msg = "no hay favoritos. Usa Ctrl+S para marcar"
        safe_addstr(stdscr, row, 0, f"  {msg}", attr_pair(CP_DIM))
    else:
        visible = filtered[scroll:scroll + PAGE_SIZE]
        for i, m in enumerate(visible):
            y = row + i
            if y >= h - 2:
                break
            is_sel = (scroll + i == sel)
            marker = "> " if is_sel else "  "
            star = "*" if m.model_id in favs else " "
            def_note = " [default]" if default_model == m.model_id else ""
            thinking_note = ""
            if m.reasoning and m.reasoning_options:
                ro = m.reasoning_options
                if any(o.get("type") == "effort" for o in ro):
                    vals = next((o["values"] for o in ro if o.get("type") == "effort"), [])
                    thinking_note = f"  think:{','.join(vals)}"
                elif any(o.get("type") == "toggle" for o in ro):
                    thinking_note = "  think:on/off"
            line = f"{marker}{star} {m.label}{def_note}{thinking_note}"
            attr = attr_pair(CP_SELECT) | attr_bold() if is_sel else curses.A_NORMAL
            safe_addstr(stdscr, y, 0, line, attr)
            if is_sel and m.model_id and m.model_id != m.label and m.model_id != "default":
                id_attr = attr_pair(CP_DIM) | attr_bold()
                safe_addstr(stdscr, y, len(line) + 2, f"  [{m.model_id}]", id_attr)
        if len(filtered) > PAGE_SIZE:
            info = f" {scroll+1}-{min(len(filtered), scroll+PAGE_SIZE)} de {len(filtered)}"
            safe_addstr(stdscr, h - 2, w - len(info) - 1, info, attr_pair(CP_DIM))
    draw_footer(stdscr, "[Enter] elegir  [Ctrl+S] fav  [Ctrl+D] default  [Ctrl+F] filtro  [Ctrl+R] recargar  [Esc] volver  escribe para buscar")


def pick_model(stdscr, provider) -> ModelItem | None:
    try:
        models = fetch_models_for_provider(provider, force_refresh=True)
    except Exception as e:
        models = []
        error = str(e)
    else:
        error = ""
    favs = load_favorites().get(provider.provider_id, [])
    default_model = get_default_model(provider)
    query = ""
    show_favs_only = False
    scroll = 0
    sel = 0
    while True:
        base = models
        if query:
            ql = query.lower()
            base = [m for m in models if ql in m.model_id.lower() or ql in m.label.lower()]
        if show_favs_only:
            filtered = [m for m in base if m.model_id in favs]
        else:
            filtered = base
        if sel >= len(filtered):
            sel = max(0, len(filtered) - 1)
        if sel < scroll:
            scroll = sel
        if sel >= scroll + PAGE_SIZE:
            scroll = sel - PAGE_SIZE + 1
        draw_model_list(stdscr, provider, models, filtered, favs, default_model,
                        query, show_favs_only, scroll, sel, error)
        stdscr.refresh()
        key = stdscr.getch()
        if key == -1:
            continue
        if key == 27:
            return None
        elif key == curses.KEY_UP:
            sel = max(0, sel - 1)
        elif key == curses.KEY_DOWN:
            sel = min(max(0, len(filtered) - 1), sel + 1)
        elif key == curses.KEY_PPAGE:
            sel = max(0, sel - PAGE_SIZE)
        elif key == curses.KEY_NPAGE:
            sel = min(max(0, len(filtered) - 1), sel + PAGE_SIZE)
        elif key == curses.KEY_HOME:
            sel = 0
        elif key == curses.KEY_END:
            sel = max(0, len(filtered) - 1)
        elif key in (curses.KEY_ENTER, 10, 13):
            if filtered:
                return filtered[sel]
        elif key == KEY_CTRL_R:
            try:
                models = fetch_models_for_provider(provider, force_refresh=True)
                error = ""
            except Exception as e:
                error = str(e)
        elif key == KEY_CTRL_S:
            if filtered:
                m = filtered[sel]
                toggle_favorite(provider.provider_id, m.model_id)
                favs = load_favorites().get(provider.provider_id, [])
        elif key == KEY_CTRL_D:
            if filtered:
                m = filtered[sel]
                set_default_model(provider, m.model_id)
                default_model = m.model_id
        elif key == KEY_CTRL_F:
            show_favs_only = not show_favs_only
            sel = 0
        elif key in (KEY_CTRL_Q, ord("q"), ord("Q")):
            return None
        elif key in (KEY_CTRL_X, curses.KEY_BACKSPACE, 127, 263):
            if query:
                query = query[:-1]
                sel = 0
                scroll = 0
        elif key in (KEY_CTRL_C, 3):
            return None
        else:
            if 32 <= key <= 126:
                query += chr(key)
                sel = 0
                scroll = 0
            elif key == ord(" "):
                query += " "
                sel = 0
                scroll = 0


def draw_thinking_list(stdscr, options, index: int, provider, model, caps_str: str, caps) -> None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    draw_header(stdscr, f"Thinking: {provider.label}")
    safe_addstr(stdscr, 3, 0, f"Modelo: {model.label}", attr_pair(CP_DIM))
    if caps_str:
        safe_addstr(stdscr, 4, 0, f"Modo: {caps_str}", attr_pair(CP_DIM))
    list_start = 6
    footer_lines = 4
    for i, (key, label) in enumerate(options):
        y = list_start + i
        if y >= h - footer_lines:
            break
        is_sel = (i == index)
        marker = "> " if is_sel else "  "
        attr = attr_pair(CP_SELECT) | attr_bold() if is_sel else curses.A_NORMAL
        safe_addstr(stdscr, y, 0, f"{marker}{i+1}. {label}", attr)
    if h - footer_lines + 1 >= 0 and index < len(options):
        sel_level = options[index][0]
        sel_params = build_thinking_params(sel_level, caps)
        preview_y = h - footer_lines
        preview = format_thinking_params(sel_params)
        safe_addstr(stdscr, preview_y, 0, f"API payload: {{ ... \"{preview}\" }}", attr_pair(CP_HINT) | attr_bold())
    draw_footer(stdscr, "[Enter] elegir  [Esc] volver  flechas para navegar")


def pick_thinking(stdscr, provider, model) -> str:
    options = get_thinking_options(model.model_id, model.reasoning, model.reasoning_options or [])
    caps = detect_thinking_capabilities(model.model_id, model.reasoning_options or [])
    caps_str = caps.label
    non_off = [o for o in options if o[0] != "off"]
    default_key = non_off[0][0] if non_off else options[0][0]
    index = 0
    for i, o in enumerate(options):
        if o[0] == default_key:
            index = i
            break
    while True:
        draw_thinking_list(stdscr, options, index, provider, model, caps_str, caps)
        stdscr.refresh()
        key = stdscr.getch()
        if key == -1:
            continue
        if key == 27:
            return ""
        if key == curses.KEY_UP:
            index = (index - 1) % len(options)
        elif key == curses.KEY_DOWN:
            index = (index + 1) % len(options)
        elif key == curses.KEY_HOME:
            index = 0
        elif key == curses.KEY_END:
            index = len(options) - 1
        elif key in (curses.KEY_ENTER, 10, 13):
            return options[index][0]
        elif key in (KEY_CTRL_Q, ord("q"), ord("Q")):
            return ""


def draw_permission_list(stdscr, options, index: int, provider) -> None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    draw_header(stdscr, f"Permisos: {provider.label}")
    for i, o in enumerate(options):
        y = 3 + i
        if y >= h - 2:
            break
        is_sel = (i == index)
        marker = "> " if is_sel else "  "
        is_danger = "dangerously" in o.label.lower()
        attr = attr_pair(CP_SELECT) | attr_bold() if is_sel else curses.A_NORMAL
        if is_danger and not is_sel:
            attr = attr_pair(CP_DANGER)
        safe_addstr(stdscr, y, 0, f"{marker}{i+1}. {o.label}", attr)
    draw_footer(stdscr, "[Enter] elegir  [Esc] volver  flechas para navegar")


def pick_permission(stdscr, provider) -> PermissionOption | None:
    options = PERMISSION_OPTIONS[provider.family]
    index = 0
    while True:
        draw_permission_list(stdscr, options, index, provider)
        stdscr.refresh()
        key = stdscr.getch()
        if key == -1:
            continue
        if key == 27:
            return None
        if key == curses.KEY_UP:
            index = (index - 1) % len(options)
        elif key == curses.KEY_DOWN:
            index = (index + 1) % len(options)
        elif key in (curses.KEY_ENTER, 10, 13):
            return options[index]
        elif key in (KEY_CTRL_Q, ord("q"), ord("Q")):
            return None


def confirm_launch(stdscr, provider, model, thinking_level, permission, slots) -> bool:
    h, w = stdscr.getmaxyx()
    stdscr.erase()
    draw_header(stdscr, "Listo para abrir Claude Code")
    caps = detect_thinking_capabilities(model.model_id, model.reasoning_options or [])
    payload = build_thinking_params(thinking_level, caps)
    catalog = fetch_models_dev_catalog(force=False)
    ctx_info = apply_context_window_env(model.model_id, catalog, provider.provider_id)
    real_ctx = ctx_info["real_ctx"] if ctx_info else 0
    known_ctx = ctx_info["known_ctx"] if ctx_info else 200000
    auto_compact = ctx_info["auto_compact"] if ctx_info else True
    threshold = (ctx_info.get("threshold") if ctx_info else None) or int(known_ctx * 0.9)
    real_str = f"{real_ctx:,}".replace(",", ".")
    known_str = f"{known_ctx:,}".replace(",", ".")
    th_str = f"{threshold:,}".replace(",", ".")
    if auto_compact:
        if real_ctx != known_ctx:
            ctx_line = f"Context:   real={real_str}  display={known_str}  compact@{th_str} (90%)"
        else:
            ctx_line = f"Context:   {known_str} tokens (auto-compact a {th_str} = 90%)"
    else:
        if real_ctx != known_ctx:
            ctx_line = f"Context:   real={real_str}  AUTO-COMPACT OFF (usar /compact manual)"
        else:
            ctx_line = f"Context:   {known_str} tokens  AUTO-COMPACT OFF (usar /compact manual)"
    lines = [
        f"Proveedor: {provider.label}",
        f"Modelo:    {model.label}  ({model.model_id})",
        ctx_line,
        f"Thinking:  {thinking_level}  ->  {format_thinking_params(payload)}",
        f"Permisos:  {permission.label}",
    ]
    for i, line in enumerate(lines):
        safe_addstr(stdscr, 3 + i, 0, line, attr_pair(CP_DIM) if i > 0 else attr_pair(CP_TITLE) | attr_bold())
    if slots and (slots.opus or slots.sonnet or slots.haiku):
        safe_addstr(stdscr, 7, 0, "Subagentes:", attr_pair(CP_TITLE) | attr_bold())
        main = model.model_id
        opus_str = slots.opus if slots.opus else f"{main} (= main)"
        sonnet_str = slots.sonnet if slots.sonnet else f"{main} (= main)"
        haiku_str = slots.haiku if slots.haiku else f"{main} (= main)"
        safe_addstr(stdscr, 8, 0, f"  opus:   {opus_str}", attr_pair(CP_DIM))
        safe_addstr(stdscr, 9, 0, f"  sonnet: {sonnet_str}", attr_pair(CP_DIM))
        safe_addstr(stdscr, 10, 0, f"  haiku:  {haiku_str}", attr_pair(CP_DIM))
    hint = ""
    if thinking_level == "off":
        hint = "Thinking desactivado (CLAUDE_CODE_DISABLE_THINKING=1)"
    elif thinking_level in CLAUDE_EFFORT_LEVELS:
        hint = f"Despues de abrir, ejecuta: /effort {thinking_level}"
    if not auto_compact and real_ctx > known_ctx:
        comp_hint = f"Auto-compact OFF. Para usar el context completo (~{real_str}), ejecuta /compact manualmente cerca del 90%."
        if hint:
            hint = f"{hint}\n{comp_hint}"
        else:
            hint = comp_hint
    if hint:
        row = 12 if slots and (slots.opus or slots.sonnet or slots.haiku) else 8
        for i, hl in enumerate(hint.split("\n")):
            safe_addstr(stdscr, row + i, 0, hl, attr_pair(CP_HINT))
    row2 = 14 if slots and (slots.opus or slots.sonnet or slots.haiku) else 10
    safe_addstr(stdscr, row2, 0, "Enter: abrir Claude  |  Esc: volver", attr_pair(CP_DIM))
    stdscr.refresh()
    key = stdscr.getch()
    if key == 27:
        return False
    if key in (curses.KEY_ENTER, 10, 13):
        return True


def draw_slots_picker(stdscr, provider, main_model, sonnet, haiku, stage) -> None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    draw_header(stdscr, f"Subagentes: {provider.label}", f"main = {main_model.label}")
    safe_addstr(stdscr, 3, 0, "  Enter: confirmar todo  Esc: usar main para todos  flechas: navegar", attr_pair(CP_DIM))
    safe_addstr(stdscr, 4, 0, "")

    section1_row = 5
    if stage == "sonnet":
        safe_addstr(stdscr, section1_row, 0, "  > slot sonnet:", attr_pair(CP_TITLE) | attr_bold())
    else:
        safe_addstr(stdscr, section1_row, 0, "    slot sonnet:", attr_pair(CP_DIM))
    sonnet_str = sonnet if sonnet else "= main (default)"
    if sonnet:
        safe_addstr(stdscr, section1_row, 16, f"  {sonnet_str}", attr_pair(CP_HINT))
    else:
        safe_addstr(stdscr, section1_row, 16, f"  {sonnet_str}", attr_pair(CP_DIM))

    section2_row = 6
    if stage == "haiku":
        safe_addstr(stdscr, section2_row, 0, "  > slot haiku:", attr_pair(CP_TITLE) | attr_bold())
    else:
        safe_addstr(stdscr, section2_row, 0, "    slot haiku:", attr_pair(CP_DIM))
    haiku_str = haiku if haiku else "= main (default)"
    if haiku:
        safe_addstr(stdscr, section2_row, 16, f"  {haiku_str}", attr_pair(CP_HINT))
    else:
        safe_addstr(stdscr, section2_row, 16, f"  {haiku_str}", attr_pair(CP_DIM))

    list_start = 9
    if stage == "sonnet":
        current = sonnet
    else:
        current = haiku

    safe_addstr(stdscr, list_start - 1, 0, f"  Modelos disponibles de {provider.label}:", attr_pair(CP_HINT))
    items = get_provider_models_for_slots(provider.provider_id)
    if not items:
        safe_addstr(stdscr, list_start, 0, "  (no hay modelos cargados; usa Tab para volver)", attr_pair(CP_DIM))
        return
    if current is None:
        marker_main = ">"
    else:
        marker_main = " "
    safe_addstr(stdscr, list_start, 0, f"  {marker_main} {main_model.model_id}  (default = main)", attr_pair(CP_SELECT) | attr_bold() if current is None else 0)
    for idx, m in enumerate(items, 1):
        row = list_start + idx
        if row >= h - 2:
            break
        is_sel = (current is not None and current == m.model_id)
        marker = ">" if is_sel else " "
        attr = attr_pair(CP_SELECT) | attr_bold() if is_sel else curses.A_NORMAL
        if m.model_id == main_model.model_id:
            safe_addstr(stdscr, row, 0, f"  {marker} {m.model_id}  (main)", attr)
            continue
        safe_addstr(stdscr, row, 0, f"  {marker} {m.model_id}", attr)
    draw_footer(stdscr, "Enter: elegir  Esc: volver al slot anterior")


def pick_agent_slots(stdscr, provider, main_model) -> AgentSlots:
    slots = AgentSlots()
    sonnet: str | None = None
    haiku: str | None = None
    stage = "sonnet"
    while True:
        draw_slots_picker(stdscr, provider, main_model, sonnet, haiku, stage)
        stdscr.refresh()
        key = stdscr.getch()
        if key == -1:
            continue
        if key in (curses.KEY_ENTER, 10, 13):
            if stage == "sonnet":
                stage = "haiku"
                continue
            return AgentSlots(sonnet=sonnet, haiku=haiku)
        if key == 27:
            if stage == "haiku":
                stage = "sonnet"
                continue
            return AgentSlots()
        if key == curses.KEY_DOWN:
            items = get_provider_models_for_slots(provider.provider_id)
            if not items:
                continue
            current = sonnet if stage == "sonnet" else haiku
            if current is None:
                if items:
                    first = items[0]
                    if stage == "sonnet":
                        sonnet = first.model_id
                    else:
                        haiku = first.model_id
                continue
            idx = next((i for i, m in enumerate(items) if m.model_id == current), -1)
            if idx < 0:
                continue
            if idx + 1 < len(items):
                chosen = items[idx + 1].model_id
                if stage == "sonnet":
                    sonnet = chosen
                else:
                    haiku = chosen
            continue
        if key == curses.KEY_UP:
            items = get_provider_models_for_slots(provider.provider_id)
            if not items:
                continue
            current = sonnet if stage == "sonnet" else haiku
            if current is None:
                continue
            idx = next((i for i, m in enumerate(items) if m.model_id == current), -1)
            if idx <= 0:
                if stage == "sonnet":
                    sonnet = None
                else:
                    haiku = None
                continue
            chosen = items[idx - 1].model_id
            if stage == "sonnet":
                sonnet = chosen
            else:
                haiku = chosen
            continue
        if key in (KEY_CTRL_Q, ord("q"), ord("Q")):
            return AgentSlots()


def apply_provider_env(provider: ProviderDefinition) -> None:
    if provider.provider_id == "codex":
        env = load_env_file(HARNESS_ENV_FILE)
        proxy = env.get("CLAUDE_HARNESS_CODEX_PROXY_URL", "").strip()
        if proxy:
            os.environ["CLAUDE_HARNESS_CODEX_PROXY_URL"] = proxy
        no_refresh = env.get("CLAUDE_HARNESS_CODEX_NO_AUTO_REFRESH", "").strip()
        if no_refresh:
            os.environ["CLAUDE_HARNESS_CODEX_NO_AUTO_REFRESH"] = no_refresh


def run_tui(stdscr, extra_args: list[str]) -> None:
    init_colors()
    try:
        fetch_models_dev_catalog(force=False)
    except Exception:
        pass
    while True:
        provider = pick_provider(stdscr)
        if provider is None:
            curses.endwin()
            print("\nChau.")
            return
        if not provider.supports_claude_launch:
            show_message(stdscr, [provider.label, "solo login/configuracion", provider.launch_block_reason or ""])
            continue
        model = pick_model(stdscr, provider)
        if model is None:
            continue
        thinking_level = pick_thinking(stdscr, provider, model)
        if not thinking_level:
            continue
        slots = pick_agent_slots(stdscr, provider, model)
        permission = pick_permission(stdscr, provider)
        if permission is None:
            continue
        if not confirm_launch(stdscr, provider, model, thinking_level, permission, slots):
            continue
        curses.endwin()
        if thinking_level == "off":
            os.environ["CLAUDE_CODE_DISABLE_THINKING"] = "1"
        catalog = fetch_models_dev_catalog(force=False)
        apply_context_window_env(model.model_id, catalog, provider.provider_id)
        if slots.opus:
            os.environ["CLAUDE_HARNESS_SLOT_OPUS"] = slots.opus
        if slots.sonnet:
            os.environ["CLAUDE_HARNESS_SLOT_SONNET"] = slots.sonnet
        if slots.haiku:
            os.environ["CLAUDE_HARNESS_SLOT_HAIKU"] = slots.haiku
        apply_provider_env(provider)
        args = [provider.launcher]
        cc_model = model_id_for_claude_code(model.model_id, provider.provider_id, catalog)
        if cc_model != "default":
            args.extend(["--model", cc_model])
        args.extend(permission.args)
        args.extend(extra_args)
        os.execv(args[0], args)
        return


def launch(provider: ProviderDefinition, model: ModelItem, thinking_level: str,
           permission: PermissionOption, slots: AgentSlots, extra_args: list[str]) -> None:
    if thinking_level == "off":
        os.environ["CLAUDE_CODE_DISABLE_THINKING"] = "1"
    catalog = fetch_models_dev_catalog(force=False)
    apply_context_window_env(model.model_id, catalog, provider.provider_id)
    if slots.opus:
        os.environ["CLAUDE_HARNESS_SLOT_OPUS"] = slots.opus
    if slots.sonnet:
        os.environ["CLAUDE_HARNESS_SLOT_SONNET"] = slots.sonnet
    if slots.haiku:
        os.environ["CLAUDE_HARNESS_SLOT_HAIKU"] = slots.haiku
    apply_provider_env(provider)
    args = [provider.launcher]
    cc_model = model_id_for_claude_code(model.model_id, provider.provider_id, catalog)
    if cc_model != "default":
        args.extend(["--model", cc_model])
    args.extend(permission.args)
    args.extend(extra_args)
    os.execv(args[0], args)


def main() -> int:
    HARNESS_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    HARNESS_FAVORITES_FILE.parent.mkdir(parents=True, exist_ok=True)

    locale.setlocale(locale.LC_ALL, "")

    cli_provider: str | None = None
    cli_model: str | None = None
    cli_dangerously_skip = False
    cli_thinking: str | None = None
    cli_slot_opus: str | None = None
    cli_slot_sonnet: str | None = None
    cli_slot_haiku: str | None = None
    extra_args: list[str] = []
    i = 1
    while i < len(sys.argv):
        arg = sys.argv[i]
        if arg == "--skip":
            os.execv(CLAUDE_NATIVE_LAUNCHER, [CLAUDE_NATIVE_LAUNCHER] + sys.argv[i + 1:])
            return 0
        elif arg == "--refresh-catalog":
            print("Descargando catalogo de models.dev...")
            catalog = fetch_models_dev_catalog(force=True)
            if catalog:
                total = sum(len(models) for models in catalog.values() if isinstance(models, dict))
                print(f"  OK: {len(catalog)} providers, {total} modelos")
            else:
                print("  ERROR: no se pudo descargar")
            return 0
        elif arg == "--provider" and i + 1 < len(sys.argv):
            cli_provider = sys.argv[i + 1]
            i += 2
        elif arg == "--model" and i + 1 < len(sys.argv):
            cli_model = sys.argv[i + 1]
            i += 2
        elif arg == "--dangerously-skip-permissions":
            cli_dangerously_skip = True
            i += 1
        elif arg == "--thinking" and i + 1 < len(sys.argv):
            cli_thinking = sys.argv[i + 1]
            i += 2
        elif arg == "--slot-opus" and i + 1 < len(sys.argv):
            cli_slot_opus = sys.argv[i + 1]
            i += 2
        elif arg == "--slot-sonnet" and i + 1 < len(sys.argv):
            cli_slot_sonnet = sys.argv[i + 1]
            i += 2
        elif arg == "--slot-haiku" and i + 1 < len(sys.argv):
            cli_slot_haiku = sys.argv[i + 1]
            i += 2
        else:
            extra_args.append(arg)
            i += 1

    if cli_provider and cli_model and cli_dangerously_skip:
        provider = find_provider(cli_provider)
        if provider and provider.supports_claude_launch:
            model = ModelItem(cli_model, cli_model)
            perm = next((o for o in PERMISSION_OPTIONS[provider.family]
                         if "dangerously" in o.label.lower()), PERMISSION_OPTIONS[provider.family][0])
            thinking = cli_thinking or "auto"
            slots = AgentSlots(opus=cli_slot_opus, sonnet=cli_slot_sonnet, haiku=cli_slot_haiku)
            launch(provider, model, thinking, perm, slots, extra_args)
            return 0

    try:
        curses.wrapper(run_tui, extra_args)
    except KeyboardInterrupt:
        try:
            curses.endwin()
        except Exception:
            pass
        print("\nInterrumpido.")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
