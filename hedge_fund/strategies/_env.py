"""零依赖的 .env 加载器（让 cron / 手动运行都能读到配置）。

为什么需要它：项目所有脚本都从 os.environ 读配置（WECHAT_WEBHOOK、API Key 等），
但之前从不加载 .env。cron 环境是最小环境，不会自动注入 .env，导致这些变量为空 →
推送被静默跳过（push_to_wechat_work 在 webhook 为空时直接 return False，不发也不报错）。

用法（在读取任何配置之前调用，且需保证本文件所在目录已在 sys.path）：
    try:
        from _env import load_env
    except ImportError:
        from hedge_fund.strategies._env import load_env
    load_env()

规则：
  - 搜索顺序： $HEDGE_FUND_ENV_FILE → 当前工作目录/.env → 从本文件向上逐级目录/.env
  - 只补充缺失/为空的变量（不覆盖已有环境变量，Shell 导出优先）
  - 容错：解析失败不影响其它变量；找不到 .env 会打印提示但不报错
"""

import os
from pathlib import Path


def _candidate_paths():
    paths = []
    env_file = os.environ.get("HEDGE_FUND_ENV_FILE")
    if env_file:
        paths.append(Path(env_file))
    paths.append(Path.cwd() / ".env")
    here = Path(__file__).resolve().parent
    # 从本文件目录向上逐级查找 .env（strategies → hedge_fund → 仓库根）
    for p in [here, *here.parents]:
        paths.append(p / ".env")
    # 去重
    seen, out = set(), []
    for p in paths:
        try:
            rp = p.resolve()
        except Exception:
            rp = p
        if rp not in seen:
            seen.add(rp)
            out.append(p)
    return out


def load_env(override: bool = False):
    """把 .env 里的键值注入 os.environ（默认只补缺失项）。返回成功加载的路径列表。"""
    loaded = []
    for p in _candidate_paths():
        if not p.is_file():
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.startswith("export "):
                        line = line[len("export "):].strip()
                    if "=" not in line:
                        continue
                    key, val = line.split("=", 1)
                    key, val = key.strip(), val.strip()
                    if len(val) >= 2 and val[0] in "\"'" and val[-1] == val[0]:
                        val = val[1:-1]
                    if not key:
                        continue
                    if override or not os.environ.get(key):
                        os.environ.setdefault(key, val)
            loaded.append(str(p))
        except Exception as e:  # 单个文件出错不影响其它
            print(f"⚠️ 加载 .env 失败 {p}: {e}")
    if loaded:
        print(f"📝 已加载 .env: {', '.join(loaded)}")
    else:
        print("⚠️ 未找到 .env 文件，配置将仅依赖环境变量/默认值（若 WECHAT_WEBHOOK 为空则不会推送）。")
    return loaded


if __name__ == "__main__":
    load_env()
