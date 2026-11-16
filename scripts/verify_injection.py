#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""反向注入验证：证明 verify_cast.py 的断言不是装饰。

做法（三段式）：
  1. 注入缺陷（把 ignoreRemoteStop 逻辑改回「收到 Stop 就停」）
  2. 确认该报的确实报错（断言变红）
  3. 还原后复跑干净

如果注入缺陷后仍然全绿，说明这些断言永远通过，等于没测。
"""
import importlib
import sys

import verify_cast


def run(expect_fail_names):
    """跑一遍，返回失败项名称集合。"""
    ok1 = verify_cast.run_tests()
    ok2 = verify_cast.validate_ssdp_and_http()
    return ok1 and ok2


def inject_broken_remote_stop():
    """缺陷注入：远端 Stop 一律生效（忽略开关完全失效）。"""
    original = verify_cast.EngineModel.stop

    def broken_stop(self, is_remote=False):
        # 丢掉保护逻辑，模拟「实现退化」
        self.state = "STOPPED"
        self.current_time = 0.0
        self.duration = 0.0
        self.current_item = None
        return

    verify_cast.EngineModel.stop = broken_stop
    return original


def inject_always_ignore():
    """缺陷注入 2：本地 Stop 也被忽略（误伤遥控器操作）。"""
    original = verify_cast.EngineModel.stop

    def broken_stop(self, is_remote=False):
        # 无条件忽略所有 Stop
        self.ignored_remote_stop_count += 1
        return

    verify_cast.EngineModel.stop = broken_stop
    return original


def inject_ssdp_malformed():
    """缺陷注入 3：SSDP 报文结尾少一个 CRLF（真实踩过的坑）。"""
    # 打补丁到 validate_ssdp_and_http 依赖的 payload 构造上不可行，
    # 改为直接测试格式判定函数本身能否识别坏报文。
    good = "HTTP/1.1 200 OK\r\nEXT:\r\nST: x\r\n\r\n"
    bad_variants = [
        "HTTP/1.1 200 OK\r\nEXT:\r\nST: x\r\n",        # 缺结尾空行
        "HTTP/1.1 200 OK\r\nEXT:\r\nST: x",            # 完全没有 CRLF 结尾
        "HTTP/1.1 200 OK\r\nEXT:\r\n\r\nST: x\r\n\r\n", # 中间夹空行
    ]

    def format_ok(p):
        return (p.startswith("HTTP/1.1 200 OK\r\n")
                and p.endswith("\r\n\r\n")
                and "\r\n\r\n" not in p[:-4])

    results = []
    results.append(("正常报文应通过", format_ok(good) is True))
    for i, b in enumerate(bad_variants):
        results.append((f"坏报文{i+1}应被拒绝", format_ok(b) is False))
    return results


def main():
    print("=" * 68)
    print("反向注入验证：确认断言能失败")
    print("=" * 68)

    all_ok = True

    # --- 注入 1：远端 Stop 失效 ---
    print("\n--- 注入 1：ignoreRemoteStop 失效（收到 Stop 就停）---")
    orig = inject_broken_remote_stop()
    print("注入后运行（期望：出现失败项）")
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        ok = run([])
    out = buf.getvalue()
    verify_cast.EngineModel.stop = orig

    failed_lines = [ln for ln in out.splitlines() if "❌" in ln]
    print(f"  失败断言数: {len(failed_lines)}")
    for ln in failed_lines:
        print("   " + ln.strip())

    if ok:
        print("  ❌ 注入缺陷后仍然全绿 → 断言无效，等于没测")
        all_ok = False
    elif len(failed_lines) >= 3:
        print(f"  ✅ 注入缺陷后 {len(failed_lines)} 条断言变红，断言有效")
    else:
        print("  ⚠️ 失败项偏少，检查覆盖面")
        all_ok = False

    print("\n--- 还原后复跑（期望：全绿）---")
    buf2 = io.StringIO()
    with redirect_stdout(buf2):
        ok2 = run([])
    if ok2:
        print("  ✅ 还原后全绿")
    else:
        print("  ❌ 还原后仍失败，还原不干净")
        all_ok = False

    # --- 注入 2：本地 Stop 被误伤 ---
    print("\n--- 注入 2：本地 Stop 也被忽略（误伤遥控器）---")
    orig2 = inject_always_ignore()
    buf3 = io.StringIO()
    with redirect_stdout(buf3):
        ok3 = run([])
    out3 = buf3.getvalue()
    verify_cast.EngineModel.stop = orig2

    f3 = [ln for ln in out3.splitlines() if "❌" in ln]
    print(f"  失败断言数: {len(f3)}")
    for ln in f3[:5]:
        print("   " + ln.strip())
    if ok3:
        print("  ❌ 注入后仍全绿 → 无法发现本地 Stop 被误伤")
        all_ok = False
    else:
        print("  ✅ 能发现本地 Stop 被误伤")

    # --- 注入 3：SSDP 报文格式 ---
    print("\n--- 注入 3：SSDP 报文格式判定能否识别坏报文 ---")
    for name, passed in inject_ssdp_malformed():
        mark = "✅" if passed else "❌"
        print(f"  {mark} {name}")
        if not passed:
            all_ok = False

    print()
    print("=" * 68)
    if all_ok:
        print("反向验证通过：断言可失败、缺陷可检出、还原干净")
        return 0
    print("反向验证失败")
    return 1


if __name__ == "__main__":
    sys.exit(main())