"""验收脚本共用的墙钟时间账单。"""
from time import perf_counter


def print_bill(title, started, stages):
    total = perf_counter() - started
    other = total - sum(cost for _, cost in stages)
    print(f"\n{title}时间账单（墙钟秒数）")
    for name, cost in [*stages, ("汇总及其余开销", other)]:
        print(f"[TIME] {name} {cost:.3f}s")
    print(f"[TIME] 总计 {total:.3f}s", flush=True)
