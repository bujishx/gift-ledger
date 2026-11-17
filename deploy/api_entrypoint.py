"""从 Compose 密钥文件装配 API 运行环境，然后执行指定命令。"""

import os
import sys
from ipaddress import IPv4Address
from pathlib import Path
from urllib.parse import quote


def read_secret(name: str) -> str:
    value = (Path("/run/secrets") / name).read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"密钥文件 {name} 为空")
    return value


def main() -> None:
    password = read_secret("mysql_app_password")
    jwt_secret = read_secret("jwt_secret")
    if len(jwt_secret.encode("utf-8")) < 32:
        raise RuntimeError("JWT 密钥至少需要 32 字节")
    os.environ["GIFT_LEDGER_DATABASE_URL"] = (
        "mysql+pymysql://gift_ledger_app:"
        f"{quote(password, safe='')}@db:3306/gift_ledger_v2?charset=utf8mb4"
    )
    os.environ["GIFT_LEDGER_JWT_SECRET"] = jwt_secret
    if "GIFT_LEDGER_TRUSTED_ORIGINS" not in os.environ and "GIFTLEDGER_SITE_IP" in os.environ:
        # An exec in a running Compose container starts with the original
        # container environment, without exports made by its startup shell.
        host = IPv4Address(os.environ["GIFTLEDGER_SITE_IP"])
        port = int(os.environ["GIFTLEDGER_HTTPS_PORT"])
        if not 1 <= port <= 65535:
            raise ValueError("本站 HTTPS 端口无效")
        os.environ["GIFT_LEDGER_TRUSTED_ORIGINS"] = (
            f"https://{host}" if port == 443 else f"https://{host}:{port}"
        )
    command = sys.argv[1:]
    if not command:
        raise RuntimeError("缺少启动命令")
    if command[0] in {"list-users", "reset-password"}:
        command = [sys.executable, "-m", "app.cli", *command]
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
