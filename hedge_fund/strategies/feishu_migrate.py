"""
飞书多维表格 一键补列 / 对齐工具
=====================================================================
用途（首次部署或飞书表结构变动时跑一次）：
  1. 列出你表格当前所有真实列名与类型，用于核对代码对齐。
  2. 自动创建缺失的风控列：止损价 / 止盈价 / 有效止损价 / 观察期天数。

使用：
  export FEISHU_APP_ID=xxx
  export FEISHU_APP_SECRET=xxx
  export FEISHU_APP_TOKEN=你的表格app_token   # 在飞书表格「文档信息」/ 用 OAuth 反查得到
  export FEISHU_TABLE_ID=tblwjeAx1Y4IQysJ
  python feishu_migrate.py            # 默认：检查 + 补列
  python feishu_migrate.py --check    # 只列出字段，不补列
"""

import argparse
import os
import sys
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from feishu_sync import (
    fetch_tenant_token, fetch_field_map, REQUIRED_NEW_COLUMNS, FEISHU_BASE,
)

APP_ID = os.environ.get("FEISHU_APP_ID", "").strip()
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "").strip()
APP_TOKEN = os.environ.get("FEISHU_APP_TOKEN", "").strip()
TABLE_ID = os.environ.get("FEISHU_TABLE_ID", "").strip()


def create_field(app_token, table_id, tenant_token, field_name, field_type):
    headers = {
        "Authorization": f"Bearer {tenant_token}",
        "Content-Type": "application/json",
    }
    r = requests.post(  # noqa: F821  (requests 在下方 import)
        f"{FEISHU_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
        headers=headers,
        json={"field_name": field_name, "type": field_type},
        timeout=10,
    ).json()
    return r


def main():
    parser = argparse.ArgumentParser(description="飞书表格补列工具")
    parser.add_argument("--check", action="store_true", help="只检查字段，不创建")
    args = parser.parse_args()

    if not (APP_ID and APP_SECRET and APP_TOKEN and TABLE_ID):
        print("❌ 请先配置环境变量: FEISHU_APP_ID / FEISHU_APP_SECRET / "
              "FEISHU_APP_TOKEN / FEISHU_TABLE_ID")
        raise SystemExit(1)

    tenant_token = fetch_tenant_token(APP_ID, APP_SECRET)
    field_map = fetch_field_map(APP_TOKEN, TABLE_ID, tenant_token)

    print(f"📋 当前表格共 {len(field_map)} 列：")
    for name, t in field_map.items():
        print(f"   - {name} (type={t})")

    print("\n🔧 缺失列检查：")
    to_create = [(n, t) for (n, t) in REQUIRED_NEW_COLUMNS if n not in field_map]
    if not to_create:
        print("   ✅ 风控列已齐全，无需补列。")
        return

    if args.check:
        print("   (--check 模式，跳过创建)")
        for n, t in to_create:
            print(f"   ➕ 将创建: {n} (type={t})")
        return

    for name, ftype in to_create:
        r = create_field(APP_TOKEN, TABLE_ID, tenant_token, name, ftype)
        if r.get("code") == 0:
            print(f"   ✅ 已创建列: {name}")
        else:
            print(f"   ❌ 创建失败 {name}: {r}")


if __name__ == "__main__":
    main()
