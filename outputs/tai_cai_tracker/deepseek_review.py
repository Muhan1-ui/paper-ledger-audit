"""Optional DeepSeek-backed narration for the read-only local review."""

import json
import os
from decimal import Decimal
from pathlib import Path
from urllib.request import Request, build_opener

from .review_agent import build_review


SYSTEM_PROMPT = """你是体彩台账的中文复盘助手。你只能根据用户提供的结构化复盘数据回答。
这是只读复盘：不得修改台账、不得编造赛果或来源、不得联网检索，不得提供购买、资金、赔率套利或保证性建议。
请严格区分“已验证事实”“冻结时模型输出”“官方结算与数据缺口”“校准状态”。
官方结算缺失或已失效时，必须明确说明无法确认赛果。每个关键结论尽量引用输入中的哈希或 URL。
输出简洁中文 Markdown，使用这四个二级标题：已验证事实、冻结时模型输出、结算与数据缺口、校准状态。"""

DEFAULT_MODEL = "deepseek-v4"
DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_LOCAL_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
_CONFIG_KEYS = ("DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL")


def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def _config_value(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def load_local_environment(environment=None, config_path=DEFAULT_LOCAL_ENV_PATH):
    """Merge an ignored project-local .env file without overriding shell variables."""
    values = dict(os.environ if environment is None else environment)
    path = Path(config_path)
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key:
            values.setdefault(key, _config_value(value))
    return values


def save_local_configuration(
    api_key,
    model=DEFAULT_MODEL,
    base_url=DEFAULT_BASE_URL,
    config_path=DEFAULT_LOCAL_ENV_PATH,
):
    """Store DeepSeek settings in the ignored project-local .env file."""
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("DEEPSEEK_API_KEY 不能为空")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("DeepSeek 模型名不能为空")
    if not isinstance(base_url, str) or not base_url.rstrip("/"):
        raise ValueError("DeepSeek API 地址不能为空")
    path = Path(config_path)
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    retained = [
        line for line in existing
        if line.strip().split("=", 1)[0].strip() not in _CONFIG_KEYS
    ]
    retained.extend((
        f"DEEPSEEK_API_KEY={api_key.strip()}",
        f"DEEPSEEK_MODEL={model.strip()}",
        f"DEEPSEEK_BASE_URL={base_url.rstrip('/')}",
    ))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(retained) + "\n", encoding="utf-8")
    return path


class DeepSeekReviewClient:
    """Small OpenAI-compatible client used only when the operator opts in."""

    def __init__(
        self,
        api_key,
        model=DEFAULT_MODEL,
        base_url=DEFAULT_BASE_URL,
        timeout=30,
        opener=None,
    ):
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("DEEPSEEK_API_KEY 未配置")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("DeepSeek 模型名不能为空")
        if not isinstance(base_url, str) or not base_url.rstrip("/"):
            raise ValueError("DeepSeek API 地址不能为空")
        self.api_key = api_key.strip()
        self.model = model.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.opener = opener or build_opener()

    @classmethod
    def from_environment(cls, environment=None, config_path=DEFAULT_LOCAL_ENV_PATH, **overrides):
        environment = load_local_environment(environment, config_path)
        return cls(
            environment.get("DEEPSEEK_API_KEY"),
            model=overrides.get("model") or environment.get("DEEPSEEK_MODEL", DEFAULT_MODEL),
            base_url=overrides.get("base_url") or environment.get(
                "DEEPSEEK_BASE_URL", DEFAULT_BASE_URL
            ),
            timeout=overrides.get("timeout", 30),
        )

    def review(self, records, match_id=None):
        review = build_review(records, match_id=match_id)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "请基于以下本地只读复盘数据生成复盘：\n"
                + json.dumps(review, ensure_ascii=False, sort_keys=True, default=_json_default),
            },
        ]
        payload = json.dumps(
            {"model": self.model, "messages": messages, "temperature": 0.2},
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(
            f"{self.base_url}/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json; charset=utf-8",
            },
            method="POST",
        )
        with self.opener.open(request, timeout=self.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise ValueError("DeepSeek 响应缺少可用文本内容") from error
        if not isinstance(content, str) or not content.strip():
            raise ValueError("DeepSeek 响应缺少可用文本内容")
        return {
            "provider": "deepseek",
            "model": self.model,
            "read_only": True,
            "review": review,
            "ai_review": content.strip(),
        }
