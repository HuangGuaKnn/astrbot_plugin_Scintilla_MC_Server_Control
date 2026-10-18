# v0.24.7 · 判据变异凭据（round 2）

`tests/test_v0247_review_round2_p2.py` 里那些「★」判据是安全边界。判据写了、跑绿了，
**不等于**它真能拦住回归 —— 一个恒真断言、一个被上游提前消费掉的副作用，都会让用例长期空转。

这份凭据的做法是：把每条边界**单点翻掉**，看对应判据会不会红。
可复跑脚本：`python tests/mutation_v0247_round2.py`（`KILLED 11/11` 才算过）。

## 结果（2026-10-18）

| 组 | 变异（把边界改成错的） | 判定 | 失败项 | 落在哪条判据上 |
|---|---|---|---|---|
| [1] | 非法 status 回落时把 note 吞掉 | KILLED | 3 | ★非法值必须**说出来** |
| [2] | 非字符串字段静默 `str()` 强转 | KILLED | 4 | `content=dict → ok:False 且点名该字段` + 「坏输入没有造出脏条目」 |
| [3] | 预设 ID 校验放行 `/`（路径穿越） | KILLED | 1 | ★`preset_path` 对非法 ID 仍抛 ValueError |
| [4] | 阻塞 IO 线程 `daemon=False` | KILLED | 2 | ★跑在 daemon 线程里 / ★兜底路径也在 daemon 线程里 |
| [5] | `stop()` 不再 `_cancel_callbacks()` | KILLED | 2 | ★stop() 取消在飞回调任务 |
| [6] | 被拒的那一条也计入字节额度 | KILLED | 1 | ★被拒的那一条**不占**额度 |
| [7] | 缓存键退回秒级 `int(st.st_mtime)` | KILLED | 4 | ★老的秒级缓存键不再命中 / 首轮键用纳秒 |
| [8]-a | 设置页 `CFG_FIELDS` 键名打错 | KILLED | 6 | `CFG_FIELDS 登记了 agent_max_task_bytes`（连带 min/max、id、后端白名单共 6 条） |
| [8]-b | schema 默认值 `262144 → 262145` | KILLED | 1 | schema 默认值与代码常量一致 |
| [8]-c | `INT_RANGES` 上限 `67108864 → 67108865` | KILLED | 1 | ★前后端上下限同源 |
| [8]-d | 设置页 `S_DEFAULTS` 塞入假默认值 | KILLED | 1 | 三键没有被塞进 S_DEFAULTS（留空=不提交） |

[8]-b / [8]-c 都是**只差 1**的单位级改动 —— 判据对边界值敏感，不是靠大改才红。

## 过程中被工具挡下来的两次

两次都不是「判据没抓到」，而是变异本身写坏了 —— 脚本把这种情形判成 `INFRA-ERR`，不冒充 KILLED：

1. `[2]` 首次变异把 `if not isinstance(...)` 那行改成了深缩进的 `raw = str(raw)`，测试在导入期
   就 `IndentationError`（`失败 0 项` 却 `rc=1`）。改成只替换 `return` 那一行后正常命中。
2. 三处多行锚点在 CRLF 工作区里 `count == 0`（`.gitattributes` 只管入库形态）。脚本改为按
   文件自身换行风格归一化锚点后再匹配。

## 安全约定

- 单点文本替换，锚点必须唯一（`count == 1`），否则跳过并记 `ANCHOR`；
- `finally` 里按**原始字节**还原，sha256 复核；
- 只动源码，不产生提交、不改版本号、不碰 `CHANGELOG.md`；
- 退出码：0 = 全部 KILLED，1 = 有 SURVIVED / 还原失败 / 锚点失效。
