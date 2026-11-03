"""
飞书多维表格 字段适配层
=====================================================================
职责：把「代码里的语义列名」映射到「你飞书表格里的真实列名」，
并根据飞书字段类型自动做值适配（数字 / 文本 / 日期）。

为什么需要它：
  - 你的表格真实列名是 `策略归属`、`建仓价格`，而代码内部语义是
    `strategy` / `entry_price`。本层做别名解析，代码写业务、列名交给配置。
  - 你的 `TrendIQ评分` 是「数字」字段(type=2)，直接写整数即可；
    `持仓收益率` 是「文本」字段，写 "0.00%" 字符串。
  - 以后你在飞书增删列，只要列名在别名表里，代码无需改。

飞书字段类型参考（部分）：
  1=文本, 2=数字, 3=单选, 4=多选, 5=日期, 7=复选框, 13=电话, 15=超链接,
  1002=货币, 1003=进度, 1004=评分, 1007=日期(增强)...
"""

import requests

# 飞书 API 根
FEISHU_BASE = "https://open.feishu.cn/open-apis"

# 语义 -> 候选真实列名（按顺序匹配，命中第一个存在于表格的列）
COLUMN_ALIASES = {
    "stock_code":    ["股票代码", "代码"],
    "stock_name":    ["股票名称", "名称"],
    "strategy":      ["策略归属", "策略桶", "匹配策略桶"],     # ← 你的真实列是「策略归属」
    "entry_price":   ["建仓价格", "买入价格", "建仓价", "最新校验价"],  # ← 真实列是「建仓价格」
    "close_price":   ["最新收盘价", "收盘价", "昨收"],
    "profit_pct":    ["持仓收益率", "收益率"],
    "hold_days":     ["持股天数"],
    "status":        ["状态"],
    "recommend_date":["推荐日期", "建仓日期"],
    "review_date":   ["复盘日期"],
    "trendiq":       ["TrendIQ评分", "TrendIQ"],
    "attribution":   ["胜负归因", "归因"],
    "stop_loss":     ["止损价", "止损"],
    "target_price":  ["止盈价", "目标价"],
    "eff_stop":      ["有效止损价"],
    "obs_days":      ["观察期天数"],
    "dividend":      ["股息率"],
    "capital":       ["主力净流入"],
    "risk":          ["风险提示"],
    "analysis":      ["多维分析"],
}

# 需要在表格里存在、缺失时由 feishu_migrate.py 自动创建的列
# (列名, 飞书字段类型)，类型含义见文件头注释
REQUIRED_NEW_COLUMNS = [
    ("止损价", 2),       # 数字
    ("止盈价", 2),       # 数字
    ("有效止损价", 2),   # 数字
    ("观察期天数", 2),   # 数字
    ("股息率", 1),       # 文本（如 5.10%）
    ("主力净流入", 1),   # 文本（如 1.23亿）
    ("风险提示", 1),     # 文本
    ("多维分析", 1),     # 文本（财报/盘面/资金面/消息面总结）
]


def fetch_tenant_token(app_id: str, app_secret: str) -> str:
    """用应用身份拿 tenant_access_token（写表用）。"""
    r = requests.post(
        f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=10,
    ).json()
    if r.get("code") != 0:
        raise RuntimeError(f"飞书鉴权失败: {r}")
    return r["tenant_access_token"]


def fetch_field_map(app_token: str, table_id: str, tenant_token: str) -> dict:
    """返回 {真实列名: 字段类型} 。"""
    headers = {"Authorization": f"Bearer {tenant_token}"}
    r = requests.get(
        f"{FEISHU_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
        headers=headers, timeout=10,
    ).json()
    if r.get("code") != 0:
        raise RuntimeError(f"读取字段失败: {r}")
    return {x["field_name"]: x["type"] for x in r.get("data", {}).get("items", [])}


def resolve_column(semantic: str, field_map: dict) -> str | None:
    """语义 -> 真实列名（命中第一个存在的）。返回 None 表示该列不存在。"""
    for cand in COLUMN_ALIASES.get(semantic, []):
        if cand in field_map:
            return cand
    return None


def adapt_value(field_type: int, value):
    """按飞书字段类型做值适配，避免类型不匹配被拒。"""
    if value is None:
        return None
    if field_type in (2, 1002, 1003):       # 数字 / 货币 / 进度
        try:
            return float(value)
        except (ValueError, TypeError):
            return None
    if field_type == 1004:                   # 评分（星级 1~5），若传整数则原样
        try:
            return int(value)
        except (ValueError, TypeError):
            return None
    if field_type in (5, 1007):              # 日期：时间戳(ms) 或 字符串
        if isinstance(value, (int, float)):
            return int(value)
        return str(value)
    # 文本 / 单选 / 多选 等 -> 统一转字符串
    return str(value)


def build_fields(raw: dict, field_map: dict) -> dict:
    """
    输入 {语义: 值}，输出飞书认可的 {真实列名: 适配后的值}，
    自动跳过表格里不存在的列。
    """
    out = {}
    for semantic, value in raw.items():
        real = resolve_column(semantic, field_map)
        if not real:
            continue  # 表格没有该列，安全跳过
        ftype = field_map.get(real)
        adapted = adapt_value(ftype, value)
        if adapted is None:
            continue
        out[real] = adapted
    return out
