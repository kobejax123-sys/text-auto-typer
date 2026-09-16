# -*- coding: utf-8 -*-
"""自测：验证结构体布局、键码解析、钩子安装，以及端到端的文本注入。

运行后会弹出一个测试窗口，脚本自动往里面输入文字并核对结果。
"""

import ctypes
import sys
import tkinter as tk

import winapi

SAMPLE = "Ab1中文-汉"
failures = []

# --- 测试辅助：把窗口强行切到前台 ---
# 走的是 winapi 里给单实例检测写的那套（Windows 有前台锁定，后台进程直接
# SetForegroundWindow 会被拒绝，焦点会留在任务栏），这里只是从 Tk 控件拿到顶层窗口句柄。
def force_foreground(widget):
    """把 Tk 窗口切到前台，返回它的顶层 HWND。"""
    hwnd = winapi.user32.GetAncestor(widget.winfo_id(), winapi.GA_ROOT)
    winapi.force_foreground_window(hwnd)
    return hwnd


def check(name, ok, detail=""):
    print("%s %s%s" % ("[通过]" if ok else "[失败]", name, ("  " + detail) if detail else ""))
    if not ok:
        failures.append(name)


# --- 1. 结构体布局（x64 下 INPUT 必须是 40 字节，否则 SendInput 会静默失败）---
check("INPUT 结构体大小为 40 字节", ctypes.sizeof(winapi.INPUT) == 40,
      "实际 %d" % ctypes.sizeof(winapi.INPUT))
check("KBDLLHOOKSTRUCT 结构体大小为 24 字节", ctypes.sizeof(winapi.KBDLLHOOKSTRUCT) == 24,
      "实际 %d" % ctypes.sizeof(winapi.KBDLLHOOKSTRUCT))

# --- 2. 键码解析 ---
cases = [("F2", 0x71), ("f12", 0x7B), ("F24", 0x87), ("A", 0x41), ("5", 0x35),
         ("NUMPAD1", 0x61), ("ENTER", 0x0D), ("ESC", 0x1B), ("SPACE", 0x20)]
for spec, expect in cases:
    check("解析热键 %s" % spec, winapi.parse_hotkey(spec) == expect,
          "得到 %s" % winapi.parse_hotkey(spec))
check("组合键被拒绝", winapi.parse_hotkey("Ctrl+F2") is None)
check("非法热键被拒绝", winapi.parse_hotkey("F99") is None)
check("键码反查名称", winapi.vk_name(0x71) == "F2" and winapi.vk_name(0x41) == "A",
      "%s / %s" % (winapi.vk_name(0x71), winapi.vk_name(0x41)))

# --- 3. 钩子安装（会被安全软件拦截的话这里就会失败）---
hook_events = []
hook = winapi.KeyboardHook(lambda vk, is_new: (hook_events.append(vk), False)[1])
try:
    hook.start()
    check("低级键盘钩子安装成功", True)
except OSError as e:
    check("低级键盘钩子安装成功", False, str(e))
hook.stop()

# --- 4. 剪贴板读写（会临时占用剪贴板，测试结束后还原）---
CLIP = "剪贴板往返测试 ClipBoard-123"
original = winapi.get_clipboard_text()
check("写入剪贴板", winapi.set_clipboard_text(CLIP))
check("读回的剪贴板内容一致", winapi.get_clipboard_text() == CLIP,
      repr(winapi.get_clipboard_text()))

# --- 5. 端到端粘贴：开一个窗口，把文字粘进去再核对 ---
LONG = "ab1中文Z9-" * 20  # 200 字符

root = tk.Tk()
root.title("注入自测")
root.geometry("420x120+200+200")
entry = tk.Entry(root, font=("Microsoft YaHei UI", 12))
entry.pack(fill="x", padx=12, pady=30)
label = tk.Label(root, text="正在等待自动输入…")
label.pack()

steps = [("短文本", SAMPLE), ("长文本 %d 字符" % len(LONG), LONG)]
state = {"failed": False, "error": ""}


def run_next():
    if not steps:
        root.after(300, root.destroy)
        return
    name, text = steps.pop(0)
    entry.delete(0, "end")
    root.update()
    label.config(text="正在粘贴：%s" % name)
    if not winapi.set_clipboard_text(text):
        state["failed"] = True
        state["error"] = "写入剪贴板失败"
    elif not winapi.send_paste():
        state["failed"] = True
        state["error"] = winapi.last_error_message()
    check_step(name, text)


def check_step(name, text, tries=25):
    # SendInput 返回时按键只是进了系统输入队列，目标程序还要花时间消化，
    # 所以不能立刻断言，要轮询到内容不再变化再比对。
    got = entry.get()
    if got != text and tries > 0:
        root.after(200, lambda: check_step(name, text, tries - 1))
        return
    check("粘贴 %s" % name, got == text,
          "期望 %d 字符，得到 %d 字符" % (len(text), len(got)))
    run_next()


def start():
    hwnd = force_foreground(root)
    entry.focus_force()
    root.update()
    foreground = winapi.foreground_window()
    check("测试窗口已切到前台", foreground == hwnd,
          "当前前台是 %r" % (winapi.window_title(foreground) or foreground))
    root.after(400, run_next)


root.after(600, start)
root.mainloop()

if state["failed"]:
    check("粘贴调用成功", False, state["error"])

if original is not None:
    winapi.set_clipboard_text(original)

print()
if failures:
    print("有 %d 项未通过：%s" % (len(failures), "、".join(failures)))
    sys.exit(1)
print("全部通过。")
