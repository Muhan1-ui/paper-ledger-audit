"""Private local configuration for the relational TaiCaiTracker service."""

from pathlib import Path
from urllib.parse import quote

from .deepseek_review import DEFAULT_LOCAL_ENV_PATH, load_local_environment


_DATABASE_KEYS = (
    "MYSQL_HOST",
    "MYSQL_PORT",
    "MYSQL_DATABASE",
    "MYSQL_USERNAME",
    "MYSQL_PASSWORD",
)


def _required_text(value, label):
    if not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value:
        raise ValueError(f"{label} 不能为空")
    return value.strip()


def save_database_configuration(
    password,
    host="127.0.0.1",
    port=3306,
    username="taicai_app",
    database="taicai_tracker",
    config_path=DEFAULT_LOCAL_ENV_PATH,
):
    """Save project-local MySQL settings to the ignored .env file."""
    host = _required_text(host, "MySQL 主机")
    username = _required_text(username, "MySQL 用户名")
    database = _required_text(database, "MySQL 数据库名")
    password = _required_text(password, "MySQL 密码")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("MySQL 端口必须在 1 到 65535 之间")
    path = Path(config_path)
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    retained = [
        line for line in existing
        if line.strip().split("=", 1)[0].strip() not in _DATABASE_KEYS
    ]
    retained.extend((
        f"MYSQL_HOST={host}",
        f"MYSQL_PORT={port}",
        f"MYSQL_DATABASE={database}",
        f"MYSQL_USERNAME={username}",
        f"MYSQL_PASSWORD={password}",
    ))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(retained) + "\n", encoding="utf-8")
    return path


def database_url_from_environment(environment=None, config_path=DEFAULT_LOCAL_ENV_PATH):
    """Return a UTF-8 MySQL SQLAlchemy URL from the private configuration."""
    values = load_local_environment(environment, config_path=config_path)
    host = _required_text(values.get("MYSQL_HOST"), "MYSQL_HOST")
    username = _required_text(values.get("MYSQL_USERNAME"), "MYSQL_USERNAME")
    password = _required_text(values.get("MYSQL_PASSWORD"), "MYSQL_PASSWORD")
    database = _required_text(values.get("MYSQL_DATABASE"), "MYSQL_DATABASE")
    try:
        port = int(values.get("MYSQL_PORT", "3306"))
    except (TypeError, ValueError) as error:
        raise ValueError("MYSQL_PORT 必须是端口号") from error
    if not 1 <= port <= 65535:
        raise ValueError("MYSQL_PORT 必须在 1 到 65535 之间")
    return (
        f"mysql+pymysql://{quote(username, safe='')}:{quote(password, safe='')}"
        f"@{host}:{port}/{quote(database, safe='')}?charset=utf8mb4"
    )
