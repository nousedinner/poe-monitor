"""Local/ops commands. Existing database upgrades are always explicit."""

import argparse
import getpass
import os
from pathlib import Path
import secrets


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-env")
    sub.add_parser("upgrade")
    admin = sub.add_parser("create-user")
    admin.add_argument("username")
    admin.add_argument("--admin", action="store_true")
    sub.add_parser("worker-health")
    args = parser.parse_args()
    if args.command == "init-env":
        path = Path(__file__).resolve().parent / ".env"
        values = {
            key: secrets.token_hex(32)
            for key in [
                "SECRET_KEY",
                "JWT_SECRET_KEY",
                "POESESSID_ENCRYPTION_KEY",
                "MYSQL_ROOT_PASSWORD",
                "MYSQL_PASSWORD",
            ]
        }
        values["DATABASE_URL"] = (
            f"mysql+pymysql://poe:{values['MYSQL_PASSWORD']}@mysql:3306/poe_monitor?charset=utf8mb4"
        )
        with path.open("x", encoding="utf8") as stream:
            stream.write(
                "\n".join(f"{key}={value}" for key, value in values.items())
                + "\nTRUST_PROXY_COUNT=1\n"
            )
        os.chmod(path, 0o600)
        print("已生成 backend/.env；密钥未显示。已有环境请保留原密钥，不要重新生成")
        return
    from app import create_app, db

    app = create_app()
    with app.app_context():
        if args.command == "upgrade":
            from app.schema import upgrade

            upgrade()
            print("数据库升级完成")
        elif args.command == "create-user":
            from app.models import User
            from werkzeug.security import generate_password_hash

            args.username = args.username.strip()
            if not args.username.strip() or len(args.username) > 50:
                raise SystemExit("用户名必须为 1–50 字符")
            password = getpass.getpass("密码：")
            if len(password) < 12:
                raise SystemExit("新密码至少 12 字符")
            if db.session.query(User).filter_by(username=args.username).first():
                raise SystemExit("用户已存在")
            db.session.add(
                User(
                    username=args.username,
                    password_hash=generate_password_hash(password),
                    is_admin=args.admin,
                )
            )
            db.session.commit()
            print("用户已创建")
        else:
            from app.models import WorkerLease
            from app.timeutils import utcnow

            lease = db.session.get(WorkerLease, 1)
            if lease is None or lease.expires_at <= utcnow():
                raise SystemExit(1)


if __name__ == "__main__":
    main()
