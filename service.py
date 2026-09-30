"""儿童口腔长期观察服务入口。

- ``python3 service.py --check``：校验配置词表与规则文件可加载
- ``python3 service.py``：启动 HTTP 服务（默认 8000 端口）
"""

from __future__ import annotations

import argparse
import json

SERVICE_ID = "pediatric-oral-followup"


def health():
    """返回稳定的服务身份。"""
    return {"status": "ok", "service": SERVICE_ID}


def self_check(db_path: str = ":memory:") -> list[str]:
    """配置与模块自检；失败时抛出异常。"""
    from app import Api, load_domain
    from rules import RULE_VERSION, rulebook_checksum
    from storage import Storage

    with open("domain.json", encoding="utf-8") as f:
        raw = json.load(f)
    assert raw["项目"] == "儿童口腔长期观察"
    domain = load_domain()
    db = Storage(db_path)
    db.initialize()
    Api(db, domain)  # 构造期会读词表，词表损坏会在此报错
    checksum = rulebook_checksum()
    assert len(checksum) == 16
    return [
        "词表加载通过",
        f"存储初始化通过（{db_path}）",
        f"规则版本 {RULE_VERSION} / 校验和 {checksum}",
    ]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="儿童口腔长期观察")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default="followup.db", help="SQLite 数据库路径")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        for line in self_check(":memory:"):
            print(line)
        print("基础检查通过")
    else:
        from app import create_server
        from storage import Storage

        db = Storage(args.db)
        db.initialize()
        server = create_server(("0.0.0.0", args.port), db)
        print(f"{SERVICE_ID} 监听 :{args.port}，数据库 {args.db}")
        server.serve_forever()
