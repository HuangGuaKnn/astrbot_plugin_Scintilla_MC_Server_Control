"""v0.23.5 回归：RCON 密码是「只写」配置 —— 不回传、不回填、不被空值抹掉。

背景（外部审查指出）：`get_settings` 会把整个摊平配置返回，`rcon_password` 明文
随之落进前端输入框。核实后还发现**两处 GPT 没提到的同源口子**：

  · `save_settings` 的响应体 `applied: parsed` 会把刚保存的密码**再明文返回一趟**
  · 密码原本在 `STR_SETTING_KEYS` 里走通用文本分支 —— 前端那个框永远是空的，
    于是「只想改个端口」的普通保存会把密码写成空串，RCON 当场断掉

本测试钉住五条契约：

  [1] `_validate_settings`：密码**非空才写**，空 / 纯空白一律**跳过该键**（不是写空）
  [2] 不改密码的普通保存，解析结果里**不存在** rcon_password 键
  [3] `rcon_password` 已移出 STR_SETTING_KEYS（否则会被通用分支写回空串）
  [4] 后端源码：get_settings 剔除密文并回 configured 布尔；applied 再抹一次；
      ignored 不把「有意跳过」的密码算成「后端未识别」
  [5] 前端源码：字段表用只写类型 p；渲染分支不回填；收集分支留空不提交；
      且全页不再出现把 cfg 里的密码写进输入框的写法

运行：
  python tests\\test_v0235_password_writeonly.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/ → _paths
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402
add_sys_paths()

from astrbot_plugin_Scintilla_MC_Server_Control.core.web_api import (   # noqa: E402
    McControlWebApi,
)

_pass = 0
_fail: list[str] = []


def check(name: str, cond: bool) -> None:
    global _pass
    if cond:
        _pass += 1
        print(f"  [ok] {name}")
    else:
        _fail.append(name)
        print(f"  [XX] {name}")


class _FakePlugin:
    """只测密码这一个键，用空壳插件即可（不触发需要插件属性的分支）。"""


def main() -> int:
    api = McControlWebApi(_FakePlugin())
    V = api._validate_settings

    print("================ [1] 非空才写，空值跳过（不是写空） ================")

    parsed, err = V({"rcon_password": "sup3r-s3cret"})
    check("新密码照常写入", err is None and parsed.get("rcon_password") == "sup3r-s3cret")

    parsed, err = V({"rcon_password": ""})
    check("空串 -> 未报错", err is None)
    check("空串 -> 结果里**没有** rcon_password 键（不会写空）",
          isinstance(parsed, dict) and "rcon_password" not in parsed)
    check("空串 -> 不是被写成了空字符串", parsed.get("rcon_password", "@@") != "")

    parsed, err = V({"rcon_password": "    "})
    check("纯空白 -> 同样跳过", err is None and "rcon_password" not in parsed)

    parsed, err = V({"rcon_password": None})
    check("None -> 同样跳过", err is None and "rcon_password" not in parsed)

    parsed, err = V({"rcon_password": "  padded  "})
    check("首尾空白被 trim", err is None and parsed.get("rcon_password") == "padded")

    print("\n================ [2] 不改密码的普通保存，不许碰密码键 ================")

    parsed, err = V({"rcon_host": "10.0.0.5", "rcon_port": 25575})
    check("只改地址端口 -> 未报错", err is None)
    check("结果里没有 rcon_password（= 保持库里原值）", "rcon_password" not in parsed)

    parsed, err = V({"rcon_host": "10.0.0.5", "rcon_password": ""})
    check("改了地址 + 密码框留空 -> 仍未写入密码键", err is None and "rcon_password" not in parsed)

    parsed, err = V({"rcon_host": "10.0.0.5", "rcon_password": "new-pwd"})
    check("改了地址 + 填了新密码 -> 两者都在",
          err is None and parsed.get("rcon_password") == "new-pwd"
          and parsed.get("rcon_host") == "10.0.0.5")

    print("\n================ [3] 密码已移出通用文本白名单 ================")

    check("rcon_password 不在 STR_SETTING_KEYS 里",
          "rcon_password" not in McControlWebApi.STR_SETTING_KEYS)

    print("\n================ [4] 后端源码：三处出口都堵上 ================")

    back = (PLUGIN_DIR / "core" / "web_api.py").read_text(encoding="utf-8")

    check('get_settings 剔除密文：flat.pop("rcon_password", None)',
          'flat.pop("rcon_password", None)' in back)
    check("get_settings 回传 rcon_password_configured 布尔",
          '"rcon_password_configured"' in back)
    check("configured 由「非空」判定，而非回传原文",
          'rcon_pwd_configured = bool(str(flat.get("rcon_password") or "").strip())' in back)
    check("save_settings 回包的 applied 也抹掉密码",
          'applied.pop("rcon_password", None)' in back)
    check("ignored 不把有意跳过的密码算成「后端未识别」",
          'k != "rcon_password"' in back)

    # 所有 json_response / 返回体里都不该直接把 settings 原文丢出去
    check("不再出现「整包配置含密码」的旧返回写法",
          '"settings": flat,' in back)

    print("\n================ [5] 前端源码：只写类型 p 的三处分支 ================")

    html = (PLUGIN_DIR / "pages" / "mc_control" / "index.html").read_text(encoding="utf-8")

    check('字段表登记为只写类型：["cfg_rcon_password","rcon_password","p"]',
          '["cfg_rcon_password","rcon_password","p"]' in html)
    check("字段表仍是旧类型 s 的说法已不存在",
          '["cfg_rcon_password","rcon_password","s"]' not in html)

    # 渲染 + 收集 各一个 p 分支，共 2 处
    n_branch = len(re.findall(r'ty==="p"', html))
    check(f'渲染与收集各有 p 分支（实际 {n_branch} 处，应为 2）', n_branch == 2)

    check("收集分支：留空不提交该键",
          "if(pv) s[key] = pv;" in html)
    check("渲染分支：写值前先 continue（绝不回填密文）",
          bool(re.search(r'else if\(ty==="p"\)\{[^}]*syncPwdPlaceholder\(false\);\s*continue;', html)))
    check("占位提示按「是否已配置」分流",
          "已配置 —— 留空则保持不变" in html and "尚未配置" in html)
    check("保存成功后刷新提示",
          "syncPwdPlaceholder(true);" in html)

    # 反向检查：页面里不该有「把 cfg 的密码写进输入框」的写法
    bad = [
        r'cfg\["rcon_password"\]\s*;?\s*$',
        r'\$\("cfg_rcon_password"\)\.value\s*=\s*cfg',
        r'el\.value\s*=\s*[^;\n]*rcon_password',
    ]
    for pat in bad:
        check(f"不存在回填密文的写法 `{pat}`", not re.search(pat, html))

    print("\n================ 汇总 ================")
    print(f"通过 {_pass} 项，失败 {len(_fail)} 项")
    if _fail:
        print("\n[XX] 失败项:")
        for f in _fail:
            print("  -", f)
        return 1
    print("\n[ok] 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
