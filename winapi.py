# -*- coding: utf-8 -*-
"""Win32 API 封装：全局低级键盘钩子 + 剪贴板读写 + Ctrl+V 粘贴。

只依赖标准库 ctypes，不需要安装任何第三方包。
"""

import ctypes
import threading
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
ULONG_PTR = ctypes.c_size_t
HHOOK = ctypes.c_void_p

# ------------------------------------------------------------------ 常量
WH_KEYBOARD_LL = 13

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_QUIT = 0x0012
WM_NULL = 0x0000

LLKHF_INJECTED = 0x10

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002

VK_CONTROL = 0x11
VK_V = 0x56

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

# ---- 系统托盘 ----
WM_APP = 0x8000
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205

NIM_ADD = 0
NIM_DELETE = 2
NIF_MESSAGE = 0x01
NIF_ICON = 0x02
NIF_TIP = 0x04
WM_TRAYICON = WM_APP + 1  # 托盘图标把鼠标事件发到这个自定义消息
TRAY_ICON_ID = 1
IDI_APPLICATION = 32512   # 系统默认程序图标，图标文件读不出来时兜底

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
SM_CXSMICON = 49
SM_CXSCREEN = 0
SM_CYSCREEN = 1
SPI_GETWORKAREA = 0x0030

MF_STRING = 0x0000
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100

GWL_WNDPROC = -4
GA_ROOT = 2

ERROR_ALREADY_EXISTS = 183  # CreateMutexW 用来判断"已经有一个实例在跑"
SW_RESTORE = 9              # ShowWindow 的参数：恢复并激活窗口

# ---- 提权（管理员权限）----
TOKEN_QUERY = 0x0008
TokenElevation = 20                     # GetTokenInformation 的查询项
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SW_SHOWNORMAL = 1                       # ShellExecuteW 的显示方式

# 修饰键单独按下时不做热键捕获，否则用户按 Ctrl 就被抓走
MODIFIER_VKS = frozenset({
    0x10, 0x11, 0x12,                    # Shift / Ctrl / Alt
    0x5B, 0x5C, 0x5D,                    # 左 Win / 右 Win / Menu
    0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5,  # 左右 Shift / Ctrl / Alt
})

# ------------------------------------------------------------------ 结构体
class KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class MOUSEINPUT(ctypes.Structure):
    # 只为把 INPUT 联合体撑到正确大小（x64 下 INPUT = 40 字节）
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = (
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    )


class _INPUTUNION(ctypes.Union):
    _fields_ = (("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT))


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = (("type", wintypes.DWORD), ("u", _INPUTUNION))


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = (
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class POINT(ctypes.Structure):
    _fields_ = (("x", wintypes.LONG), ("y", wintypes.LONG))


class RECT(ctypes.Structure):
    _fields_ = (("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG))


class GUID(ctypes.Structure):
    _fields_ = (
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_byte * 8),
    )


class NOTIFYICONDATAW(ctypes.Structure):
    """Shell_NotifyIcon 的参数。字段顺序和大小必须和 Windows 头文件完全一致。"""

    _fields_ = (
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", ctypes.c_wchar * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HICON),
    )


class TOKEN_ELEVATION(ctypes.Structure):
    """GetTokenInformation(TokenElevation) 的返回，一个 BOOL 打包成 DWORD。"""

    _fields_ = [("TokenIsElevated", wintypes.DWORD)]


HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD)
user32.SetWindowsHookExW.restype = HHOOK
user32.CallNextHookEx.argtypes = (HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = (HHOOK,)
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
user32.GetMessageW.restype = ctypes.c_int
user32.PostThreadMessageW.argtypes = (wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
user32.PostMessageW.restype = wintypes.BOOL
user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT
user32.GetForegroundWindow.restype = wintypes.HWND
user32.IsWindowVisible.argtypes = (wintypes.HWND,)
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
user32.GetClassNameW.restype = ctypes.c_int
user32.WindowFromPoint.argtypes = (POINT,)
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(RECT))
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
user32.GetWindowTextW.restype = ctypes.c_int
kernel32.GetCurrentThreadId.restype = wintypes.DWORD

user32.IsClipboardFormatAvailable.argtypes = (wintypes.UINT,)
user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
user32.OpenClipboard.argtypes = (wintypes.HWND,)
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = (wintypes.UINT,)
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = (wintypes.UINT, wintypes.HANDLE)
user32.SetClipboardData.restype = wintypes.HANDLE

kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
kernel32.GlobalLock.restype = wintypes.LPVOID
kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.GlobalFree.argtypes = (wintypes.HGLOBAL,)
kernel32.GlobalFree.restype = wintypes.HGLOBAL

user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
user32.GetAncestor.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
user32.GetSystemMetrics.restype = ctypes.c_int
user32.LoadImageW.argtypes = (wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                              ctypes.c_int, ctypes.c_int, wintypes.UINT)
user32.LoadImageW.restype = wintypes.HANDLE
user32.LoadIconW.argtypes = (wintypes.HINSTANCE, wintypes.LPCWSTR)
user32.LoadIconW.restype = wintypes.HICON
user32.DestroyIcon.argtypes = (wintypes.HICON,)
user32.DestroyIcon.restype = wintypes.BOOL
# 32 位 Python 的 user32 里没有 SetWindowLongPtrW（它在头文件里只是个宏）
# 第三个参数用 c_void_p 而不是 WNDPROC：设置时传回调指针，恢复时传回来的是整数地址
_SetWindowLongPtrW = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
_SetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_void_p)
_SetWindowLongPtrW.restype = ctypes.c_void_p
user32.CallWindowProcW.argtypes = (ctypes.c_void_p, wintypes.HWND, wintypes.UINT,
                                   wintypes.WPARAM, wintypes.LPARAM)
user32.CallWindowProcW.restype = LRESULT
user32.GetCursorPos.argtypes = (ctypes.POINTER(POINT),)
user32.GetCursorPos.restype = wintypes.BOOL
user32.SystemParametersInfoW.argtypes = (wintypes.UINT, wintypes.UINT,
                                         ctypes.c_void_p, wintypes.UINT)
user32.SystemParametersInfoW.restype = wintypes.BOOL
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.argtypes = (wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR)
user32.AppendMenuW.restype = wintypes.BOOL
user32.TrackPopupMenu.argtypes = (wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, wintypes.HWND, ctypes.c_void_p)
user32.TrackPopupMenu.restype = ctypes.c_int
user32.DestroyMenu.argtypes = (wintypes.HMENU,)
user32.DestroyMenu.restype = wintypes.BOOL
shell32.Shell_NotifyIconW.argtypes = (wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW))
shell32.Shell_NotifyIconW.restype = wintypes.BOOL

kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
kernel32.CloseHandle.restype = wintypes.BOOL
user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
user32.FindWindowW.restype = wintypes.HWND
user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
user32.ShowWindow.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.AttachThreadInput.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
user32.AttachThreadInput.restype = wintypes.BOOL
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
kernel32.OpenProcess.restype = wintypes.HANDLE
advapi32.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD,
                                      ctypes.POINTER(wintypes.HANDLE))
advapi32.OpenProcessToken.restype = wintypes.BOOL
advapi32.GetTokenInformation.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                         wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
advapi32.GetTokenInformation.restype = wintypes.BOOL
shell32.ShellExecuteW.argtypes = (wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                  wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int)
shell32.ShellExecuteW.restype = ctypes.c_ssize_t


# ------------------------------------------------------------------ 键码表
_NAMED_VKS = {
    "BACKSPACE": 0x08, "TAB": 0x09, "ENTER": 0x0D, "RETURN": 0x0D,
    "ESC": 0x1B, "ESCAPE": 0x1B, "SPACE": 0x20,
    "PAGEUP": 0x21, "PGUP": 0x21, "PAGEDOWN": 0x22, "PGDN": 0x22,
    "END": 0x23, "HOME": 0x24,
    "LEFT": 0x25, "UP": 0x26, "RIGHT": 0x27, "DOWN": 0x28,
    "INSERT": 0x2D, "INS": 0x2D, "DELETE": 0x2E, "DEL": 0x2E,
    "CAPSLOCK": 0x14, "NUMLOCK": 0x90, "SCROLLLOCK": 0x91,
    "PAUSE": 0x13, "PRINTSCREEN": 0x2C,
    "NUMPAD0": 0x60, "NUMPAD1": 0x61, "NUMPAD2": 0x62, "NUMPAD3": 0x63,
    "NUMPAD4": 0x64, "NUMPAD5": 0x65, "NUMPAD6": 0x66, "NUMPAD7": 0x67,
    "NUMPAD8": 0x68, "NUMPAD9": 0x69,
    "NUMPAD*": 0x6A, "NUMPAD+": 0x6B, "NUMPAD-": 0x6D, "NUMPAD.": 0x6E, "NUMPAD/": 0x6F,
    ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF,
    "`": 0xC0, "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
}
for _i in range(1, 25):
    _NAMED_VKS["F%d" % _i] = 0x6F + _i
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789":
    _NAMED_VKS[_c] = ord(_c)

_VK_TO_NAME = {}
for _name, _vk in _NAMED_VKS.items():
    # 同码别名取更常见、更短的写法
    if _vk not in _VK_TO_NAME or len(_name) < len(_VK_TO_NAME[_vk]):
        _VK_TO_NAME[_vk] = _name


def parse_hotkey(spec):
    """把 "F2" / "F12" / "NUMPAD1" 这类单键写法转成虚拟键码。

    只支持单键；无法识别或带 "+" 的组合键返回 None。
    """
    if not spec:
        return None
    key = spec.strip().upper()
    if "+" in key:
        return None
    # 表里已经含了 A-Z、0-9 以及 ; = , - . / ` [ \ ] ' 这些单字符，所以单键查
    # 这一张表就够了，不用再按长度分别处理
    return _NAMED_VKS.get(key)


def vk_name(vk):
    """虚拟键码转显示名，未知键返回 0xNN 形式。"""
    return _VK_TO_NAME.get(vk) or "0x%02X" % vk


# ------------------------------------------------------------------ 键盘输入注入
def _key_event(vk=0, scan=0, flags=0):
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
    return inp


def _send(events):
    """一次 SendInput 调用发出全部事件。失败（多为目标窗口权限更高）返回 False。"""
    if not events:
        return True
    n = len(events)
    arr = (INPUT * n)(*events)
    return user32.SendInput(n, arr, ctypes.sizeof(INPUT)) == n


def send_paste():
    """发送一次 Ctrl+V。"""
    return _send([
        _key_event(vk=VK_CONTROL),
        _key_event(vk=VK_V),
        _key_event(vk=VK_V, flags=KEYEVENTF_KEYUP),
        _key_event(vk=VK_CONTROL, flags=KEYEVENTF_KEYUP),
    ])


# ------------------------------------------------------------------ 剪贴板
def _open_clipboard(retries=5):
    """剪贴板同一时刻只允许一个进程打开，被别的程序占用时重试几次。"""
    for _ in range(retries):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.02)
    return False


def get_clipboard_text():
    """读取剪贴板里的文本。里面不是文本、或被别的程序占着时返回 None。"""
    if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
        return None
    if not _open_clipboard():
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text):
    """把文本写入剪贴板，返回是否成功。

    先把数据准备好再清空剪贴板：清空之后才失败的话，用户原来复制的东西就没了。
    """
    data = text.encode("utf-16-le") + b"\x00\x00"  # CF_UNICODETEXT 要以空字符结尾
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not handle:
        return False
    ptr = kernel32.GlobalLock(handle)
    if not ptr:
        kernel32.GlobalFree(handle)
        return False
    ctypes.memmove(ptr, data, len(data))
    kernel32.GlobalUnlock(handle)

    if not _open_clipboard():
        kernel32.GlobalFree(handle)
        return False
    try:
        if not user32.EmptyClipboard():
            kernel32.GlobalFree(handle)
            return False
        # 交给系统后所有权就转移了，成功时不能再 GlobalFree
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            return False
        return True
    finally:
        user32.CloseClipboard()


def last_error_message():
    code = ctypes.get_last_error()
    return "%s (错误码 %d)" % (ctypes.FormatError(code).strip(), code)


def foreground_window():
    return user32.GetForegroundWindow()


def window_title(hwnd):
    if not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def window_class(hwnd):
    """窗口的类名。有些窗口没有标题（隐藏的辅助窗口尤其如此），类名是唯一的线索。"""
    if not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def window_rect(hwnd):
    """窗口外框在屏幕上的位置，返回 (left, top, right, bottom)，取不到返回 None。"""
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return rect.left, rect.top, rect.right, rect.bottom


def window_at_point(x, y):
    """屏幕某个坐标上最顶层的窗口。用来看自己的窗口有没有被别人盖住。"""
    hwnd = user32.WindowFromPoint(POINT(x, y))
    return user32.GetAncestor(hwnd, GA_ROOT) if hwnd else None


def work_area():
    """返回主屏的工作区 (left, top, right, bottom)，也就是去掉任务栏之后的范围。

    拿它居中而不是拿整块屏幕：任务栏占着底部，按整屏算出来的位置看着是偏下的。
    """
    rect = RECT()
    if user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
        return rect.left, rect.top, rect.right, rect.bottom
    return 0, 0, user32.GetSystemMetrics(SM_CXSCREEN), user32.GetSystemMetrics(SM_CYSCREEN)


# ------------------------------------------------------------------ 键盘钩子
class KeyboardHook:
    """全局低级键盘钩子。

    回调在钩子线程里执行，Windows 要求它尽快返回，所以调用方只允许在
    on_key 里做查表和入队，绝不能碰 tkinter。
    """

    def __init__(self, on_key):
        # on_key(vk, is_new_press) -> bool，返回 True 表示吞掉这次按键
        self._on_key = on_key
        self.capture = None          # 非 None 时进入按键捕获模式，值为 callable(vk)

        self._thread = None
        self._thread_id = 0
        self._hook = None
        self._proc = None            # 必须持有引用，否则回调被 GC 回收
        self._ready = threading.Event()
        self._error = None
        self._down = set()

    def start(self):
        self._thread = threading.Thread(target=self._run, name="keyboard-hook", daemon=True)
        self._thread.start()
        if not self._ready.wait(5):
            # 抛 OSError 是为了和下面那条失败路径统一：调用方只需要捕获一种异常，
            # 抛别的类型会一路穿出去，在 pythonw 下表现为进程静默消失、什么都没有。
            raise OSError("启动超时")
        if self._error:
            raise self._error

    def stop(self):
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)

    # -- 钩子线程 --
    def _run(self):
        self._thread_id = kernel32.GetCurrentThreadId()
        self._proc = HOOKPROC(self._hook_proc)
        self._hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, None, 0)
        if not self._hook:
            self._error = ctypes.WinError(ctypes.get_last_error())
        self._ready.set()
        if self._error:
            return

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        user32.UnhookWindowsHookEx(self._hook)
        self._hook = None

    def _hook_proc(self, n_code, w_param, l_param):
        if n_code == 0:
            kb = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            # 自己注入的按键带有 INJECTED 标志，直接放行，避免递归触发
            if not (kb.flags & LLKHF_INJECTED):
                vk = kb.vkCode
                if w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    # 捕获模式下修饰键照样放行，否则用户按 Ctrl 就被抓走
                    if self.capture is not None and vk not in MODIFIER_VKS:
                        self.capture(vk)
                        return 1  # 捕获只是用来设置热键，不记进 _down 的按键状态
                    # 其余情况都要记下按下状态，_on_key 返回 True 表示这个键归我们
                    swallowed = (self.capture is None
                                 and self._on_key(vk, vk not in self._down))
                    self._down.add(vk)
                    if swallowed:
                        return 1
                elif w_param in (WM_KEYUP, WM_SYSKEYUP):
                    self._down.discard(vk)
        return user32.CallNextHookEx(None, n_code, w_param, l_param)


# ------------------------------------------------------------------ 系统托盘
def show_popup_menu(hwnd, items):
    """在鼠标位置弹出右键菜单，返回被点中项的 id，没选中返回 0。

    items 是 [(id, 文字), ...]。弹出前必须先把这个窗口设为前台，
    否则菜单在点击别处时不会消失。
    """
    menu = user32.CreatePopupMenu()
    if not menu:
        return 0
    try:
        for item_id, label in items:
            user32.AppendMenuW(menu, MF_STRING, item_id, label)
        point = POINT()
        user32.GetCursorPos(ctypes.byref(point))
        user32.SetForegroundWindow(hwnd)
        command = user32.TrackPopupMenu(
            menu, TPM_RIGHTBUTTON | TPM_RETURNCMD, point.x, point.y, 0, hwnd, None
        )
        # 文档要求的收尾动作：菜单关闭后补一条空消息，否则菜单可能留在屏幕上不消失
        user32.PostMessageW(hwnd, WM_NULL, 0, 0)
        return command
    finally:
        user32.DestroyMenu(menu)


class TrayIcon:
    """系统托盘图标。

    实现方式是子类化宿主窗口的窗口过程来接收托盘消息，因此回调都在 Tk 主线程里执行，
    可以直接操作界面，不需要额外的线程和队列。
    """

    def __init__(self, tk_widget, icon_path, tooltip="", on_activate=None, on_menu=None):
        self._hwnd = user32.GetAncestor(tk_widget.winfo_id(), GA_ROOT)
        self._on_activate = on_activate
        self._on_menu = on_menu
        self._proc = WNDPROC(self._wnd_proc)  # 必须留引用，否则回调被回收
        self._old_proc = None
        self._active = False

        self._data = NOTIFYICONDATAW()
        self._data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        self._data.hWnd = self._hwnd
        self._data.uID = TRAY_ICON_ID
        self._data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        self._data.uCallbackMessage = WM_TRAYICON
        self._data.szTip = tooltip[:127]
        self._data.hIcon = self._load_icon(icon_path)

        self._old_proc = _SetWindowLongPtrW(
            self._hwnd, GWL_WNDPROC, ctypes.cast(self._proc, ctypes.c_void_p)
        )
        self._active = bool(shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._data)))
        if not self._active:
            self._restore_proc()

    @property
    def active(self):
        return self._active

    @property
    def hwnd(self):
        """宿主窗口句柄，弹托盘菜单时要传给 TrackPopupMenu。"""
        return self._hwnd

    def close(self):
        if self._active:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._data))
            self._active = False
        self._restore_proc()
        if self._data.hIcon:
            user32.DestroyIcon(self._data.hIcon)
            self._data.hIcon = None

    def _load_icon(self, path):
        size = user32.GetSystemMetrics(SM_CXSMICON)
        handle = user32.LoadImageW(None, path, IMAGE_ICON, size, size, LR_LOADFROMFILE)
        if not handle:
            # 图标文件缺失时退回系统默认图标，至少托盘里有个能点的地方
            handle = user32.LoadIconW(None, ctypes.c_wchar_p(IDI_APPLICATION))
        return handle

    def _restore_proc(self):
        if self._old_proc:
            _SetWindowLongPtrW(self._hwnd, GWL_WNDPROC, self._old_proc)
            self._old_proc = None

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAYICON:
            event = lparam & 0xFFFF
            if event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                if self._on_activate:
                    self._on_activate()
            elif event == WM_RBUTTONUP:
                if self._on_menu:
                    self._on_menu()
            return 0
        # 其余消息一律交回 Tk 原来的窗口过程，否则界面会失灵
        return user32.CallWindowProcW(self._old_proc, hwnd, msg, wparam, lparam)


# ------------------------------------------------------------------ 单实例与窗口激活
def acquire_single_instance(name):
    """抢一个命名互斥体，用来判断是否已经有实例在运行。

    返回互斥体句柄，调用方要一直持有它（进程退出时系统自动释放）。
    已经有实例在跑、或者因为对方权限更高而打不开时返回 None。
    """
    handle = kernel32.CreateMutexW(None, False, name)
    error = ctypes.get_last_error()
    if handle and error != ERROR_ALREADY_EXISTS:
        return handle
    if handle:
        kernel32.CloseHandle(handle)
    return None


def acquire_single_instance_with_wait(name, timeout):
    """带等待的单实例互斥。

    提权重启用的：老实例收到「用户已同意 UAC」才退出，互斥体要等它的进程
    真正消失才放开，这里轮询等到超时为止。等不到就按「已有实例在跑」返回 None。
    """
    deadline = time.monotonic() + timeout
    while True:
        handle = acquire_single_instance(name)
        if handle:
            return handle
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.2)


def find_window_by_title(title):
    """按标题精确匹配找窗口，找不到返回空。"""
    return user32.FindWindowW(None, title)


def force_foreground_window(hwnd):
    """把窗口显示出来并切到前台。

    Windows 有前台锁定，后台进程直接调 SetForegroundWindow 会被拒绝，
    这里先把自己的线程挂到当前前台线程上，切换完再解挂。
    SW_RESTORE 会把隐藏着的窗口重新显示出来——这正是想要的：重复启动时，
    把藏在托盘里的老实例叫出来靠的就是这一下（用户亲手双击了图标，显示是应该的）。
    """
    if not hwnd:
        return False
    user32.ShowWindow(hwnd, SW_RESTORE)
    if user32.GetForegroundWindow() == hwnd:
        return True
    foreground_thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
    this_thread = kernel32.GetCurrentThreadId()
    attached = user32.AttachThreadInput(this_thread, foreground_thread, True)
    try:
        return bool(user32.SetForegroundWindow(hwnd))
    finally:
        if attached:
            user32.AttachThreadInput(this_thread, foreground_thread, False)


# ------------------------------------------------------------------ 提权
def _token_elevated(handle):
    """进程句柄 -> 它的令牌是不是提升（管理员）过的。查不到返回 None。"""
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(handle, TOKEN_QUERY, ctypes.byref(token)):
        return None
    try:
        info = TOKEN_ELEVATION()
        size = wintypes.DWORD()
        if not advapi32.GetTokenInformation(token, TokenElevation, ctypes.byref(info),
                                            ctypes.sizeof(info), ctypes.byref(size)):
            return None
        return bool(info.TokenIsElevated)
    finally:
        kernel32.CloseHandle(token)


def is_elevated():
    """本进程当前是不是管理员（提升）权限。

    管理员组的用户默认也是过 UAC 过滤的普通令牌，所以「账户是 Administrator」
    和「进程已提权」是两回事，这里查的是后者。
    """
    return _token_elevated(kernel32.GetCurrentProcess()) is True


def window_elevated(hwnd):
    """某个窗口的属主进程是不是管理员权限。

    粘贴失败时用来判断是不是 UIPI 拦的：SendInput 被拦只返回 0，错误码不可靠
    （文档明说看不出来），只能自己去查目标窗口属主的令牌。
    """
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return None
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return None
    try:
        return _token_elevated(handle)
    finally:
        kernel32.CloseHandle(handle)


def shell_runas(exe, params="", directory=""):
    """以管理员身份启动一个程序（会弹一次 UAC）。返回用户是否点了「是」。"""
    return shell32.ShellExecuteW(None, "runas", exe, params, directory, SW_SHOWNORMAL) > 32
