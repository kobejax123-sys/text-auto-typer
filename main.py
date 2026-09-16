# -*- coding: utf-8 -*-
"""文本自动输入器：按下热键，把对应文本输入到当前焦点窗口。"""

import json
import os
import queue
import subprocess
import sys
import time
import tkinter as tk
from tkinter import messagebox, ttk
from xml.sax.saxutils import escape

import winapi

APP_NAME = "文本自动输入器"
VERSION = "v1.0.0"  # 带 v 前缀，和 git tag 的命名保持一致
CONFIG_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "TextAutoTyper")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
LOG_PATH = os.path.join(CONFIG_DIR, "startup.log")
LOG_MAX_BYTES = 256 * 1024
# 用脚本所在目录定位，这样从桌面快捷方式启动（工作目录可能是别处）也能找到
ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.ico")

FONT = ("Microsoft YaHei UI", 9)
POLL_MS = 15
CLIPBOARD_RESTORE_MS = 300  # 粘贴后隔多久把剪贴板内容还原

CLOSE_TRAY = "tray"  # 关闭窗口时缩到托盘，程序继续在后台
CLOSE_QUIT = "quit"  # 关闭窗口即退出
DEFAULT_CLOSE_ACTION = CLOSE_TRAY
TRAY_CLOSE_LABEL = "最小化到托盘（后台继续运行）"  # 托盘不可用时按它找到该菜单项并置灰

TRAY_SHOW = 1
TRAY_QUIT = 2
TRAY_ELEVATE = 3

# 单实例用的互斥体名字
MUTEX_NAME = "TextAutoTyper.SingleInstance"

# 开机自启：一个最高权限的登录计划任务，开机后静默提权、待在托盘（详见 set_autostart）
AUTOSTART_TASK = "TextAutoTyperLogon"
# 双击桌面图标时，已在跑的实例（多半是托盘里的管理员实例）靠这个文件收到
# 「把窗口亮出来」的信号——普通进程没法操作管理员实例的窗口，只能递话
SHOW_FLAG = os.path.join(CONFIG_DIR, "show.flag")

# 打字离不开的键：设成热键后在其他程序里就按不出来了，所以要多问一句。
# 功能键（F1~F24、方向键等）不在此列，用户拿它们当热键是预期内的。
TYPING_KEYS = (frozenset(range(0x41, 0x5B))      # A-Z
               | frozenset(range(0x30, 0x3A))    # 0-9
               | frozenset({0x20, 0x08, 0x09, 0x0D}))  # 空格、退格、Tab、回车

HINT = "提示：双击任意一行可将该行文本复制到剪贴板中，然后您可以将其粘贴到任意输入框中。"


# ---------------------------------------------------------------- 配置读写
def load_config():
    """读取配置，返回 (close_action, items, warning)。

    文件不存在算首次运行，静默返回默认值；文件损坏时把它改名备份并返回一条警告，
    否则每次启动都会重复报同一个错，而用户也不知道该删哪个文件。
    """
    close_action = DEFAULT_CLOSE_ACTION
    items = []
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return close_action, items, None
    except ValueError as e:
        # 内容确实坏了（JSON 解析不了、编码错乱），不搬走的话每次启动都报同一个错
        warning = "配置文件读取失败：%s" % e
        try:
            os.replace(CONFIG_PATH, CONFIG_PATH + ".bak")
            warning += "\n\n已把它改名为 config.json.bak，本次先用空配置启动。"
        except OSError:
            pass
        return close_action, items, warning
    except OSError as e:
        # 文件本身可能没坏，只是暂时读不到（被扫描、被同步盘锁住、权限不足）。
        # 这种情况不能当成损坏处理，否则会把好配置搬走、用户的绑定全消失。
        return close_action, items, (
            "配置文件暂时读不出来：%s\n\n本次先用空配置启动。文件已原样保留，"
            "重启程序通常就能读到。" % e
        )

    if not isinstance(data, dict):
        return close_action, items, "配置文件内容不是预期的格式，本次先用空配置启动。"

    if data.get("close_action") in (CLOSE_TRAY, CLOSE_QUIT):
        close_action = data["close_action"]

    for raw in data.get("items", []):
        if not isinstance(raw, dict):
            continue
        hotkey = winapi.vk_name(vk) if (vk := winapi.parse_hotkey(str(raw.get("hotkey", "")))) else ""
        text = str(raw.get("text", ""))
        if hotkey and text:
            items.append({"hotkey": hotkey, "text": text})
    return close_action, items, None


def save_config(close_action, items):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({"close_action": close_action, "items": items}, f,
                  ensure_ascii=False, indent=2)


def log(message):
    """往启动日志里追加一行。

    程序平时悄悄跑，偶发的问题（窗口没开到前台之类）又很难复现，留个流水账至少能
    事后看清当时发生了什么：启动那会儿用户在用什么窗口、抢前台抢到没有。日志写不
    进去就算了，绝不能因为它影响程序本身。
    """
    try:
        if os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            os.remove(LOG_PATH)  # 免得无限长大，超了就从零开始
    except OSError:
        pass
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("%s [pid %d] %s\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid(), message))
    except OSError:
        pass


def _schtasks(*args):
    """跑一次 schtasks，返回 (是否成功, 输出文本)。

    CREATE_NO_WINDOW 是为了不闪那个黑框——程序是以 pythonw 跑的，没有控制台。
    编码必须是 mbcs：schtasks 吐的是系统 ANSI 代码页（中文 Windows 上是 GBK），
    按 UTF-8 解会直接抛错，输出全成了乱码。
    """
    try:
        done = subprocess.run(
            ["schtasks", *args], capture_output=True, text=True,
            encoding="mbcs", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except OSError as e:
        return False, str(e)
    return done.returncode == 0, (done.stdout or "") + (done.stderr or "")


def _autostart_xml():
    """登录计划任务的 XML。

    开机要静默拿到管理员权限，Run 键和启动文件夹都做不到（Vista 起就被禁了），
    唯一正路就是这里的 RunLevel Highest——把 UAC 的同意前置到建任务这一刻。
    触发器限定当前用户登录，免得别的账户登录也被拉起来。
    """
    user = "%s\\%s" % (os.environ.get("USERDOMAIN", ""),
                       os.environ.get("USERNAME", ""))
    if getattr(sys, "frozen", False):  # 被 PyInstaller 打包成单个 exe 时
        command, arguments = sys.executable, "--tray"
    else:
        command = sys.executable
        arguments = '"%s" --tray' % os.path.abspath(__file__)
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        '  <Triggers>\n'
        '    <LogonTrigger>\n'
        '      <Enabled>true</Enabled>\n'
        '      <UserId>%s</UserId>\n'
        '    </LogonTrigger>\n'
        '  </Triggers>\n'
        '  <Settings>\n'
        '    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n'
        '    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n'
        '    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n'
        '    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n'
        '    <Enabled>true</Enabled>\n'
        '  </Settings>\n'
        '  <Principals>\n'
        '    <Principal>\n'
        '      <UserId>%s</UserId>\n'
        '      <LogonType>Interactive</LogonType>\n'
        '      <RunLevel>Highest</RunLevel>\n'
        '    </Principal>\n'
        '  </Principals>\n'
        '  <Actions>\n'
        '    <Exec>\n'
        '      <Command>%s</Command>\n'
        '      <Arguments>%s</Arguments>\n'
        '      <WorkingDirectory>%s</WorkingDirectory>\n'
        '    </Exec>\n'
        '  </Actions>\n'
        '</Task>\n'
    ) % (escape(user), escape(user), escape(command), escape(arguments),
         escape(os.path.dirname(os.path.abspath(__file__))))


def autostart_state():
    """开机自启开了没有：那个登录计划任务还在不在。

    只看 /query 的返回码，不解析输出——输出是 GBK 又带本地化文本，
    解析它踩过坑（关掉了还显示勾选），返回码没这些问题。
    """
    ok, _ = _schtasks("/query", "/tn", AUTOSTART_TASK)
    return ok


def set_autostart(enable):
    """建/删登录计划任务，返回 (是否成功, 出错信息)。两头的改动都需要管理员权限。"""
    if enable:
        xml_path = os.path.join(CONFIG_DIR, "autostart.xml")
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(xml_path, "w", encoding="utf-16") as f:
                f.write(_autostart_xml())
            ok, out = _schtasks("/create", "/tn", AUTOSTART_TASK,
                                "/xml", xml_path, "/f")
        except OSError as e:
            return False, str(e)
        finally:
            try:
                os.remove(xml_path)
            except OSError:
                pass
        return ok, out.strip()
    if not autostart_state():
        return True, ""  # 本来就没有，等于已经关上
    ok, out = _schtasks("/delete", "/tn", AUTOSTART_TASK, "/f")
    return ok, out.strip()


def request_show():
    """给已在跑的实例留一个「把窗口亮出来」的信号。

    双击桌面图标时，新起的普通权限实例发现管理员实例已在跑：UIPI 不许它
    直接去动管理员实例的窗口，只能留下这个文件，由对方自己把窗口亮出来
    （它轮询得很快，人眼无感）。写不进去就算了，双击等于没反应。
    """
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(SHOW_FLAG, "w"):
            pass
    except OSError:
        pass


# ---------------------------------------------------------------- 添加/修改对话框
class ItemDialog(tk.Toplevel):
    def __init__(self, master, hook, title, conflict_check, hotkey="", text=""):
        super().__init__(master)
        self.hook = hook
        self.conflict_check = conflict_check
        self.result = None

        self._cap_queue = queue.Queue()
        self._capturing = False
        self._alive = True

        self.title(title)
        self.resizable(False, False)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        self.hotkey_var = tk.StringVar(value=hotkey)
        self.text_var = tk.StringVar(value=text)

        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)

        ttk.Label(body, text="热键：").grid(row=0, column=0, sticky="w", pady=4)
        hotkey_entry = ttk.Entry(body, textvariable=self.hotkey_var, width=14, state="readonly")
        hotkey_entry.grid(row=0, column=1, sticky="w", pady=4)
        self.capture_btn = ttk.Button(body, text="按下按键…", width=14, command=self._start_capture)
        self.capture_btn.grid(row=0, column=2, sticky="w", padx=(8, 0), pady=4)

        ttk.Label(body, text="文本：").grid(row=1, column=0, sticky="w", pady=4)
        text_entry = ttk.Entry(body, textvariable=self.text_var, width=42)
        text_entry.grid(row=1, column=1, columnspan=2, sticky="ew", pady=4)

        ttk.Label(
            body,
            text="只支持单键热键（F1~F24、字母、数字、小键盘等）。",
            foreground="#666666",
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))

        buttons = ttk.Frame(self, padding=(12, 0, 12, 12))
        buttons.pack(fill="x")
        ttk.Button(buttons, text="取消", width=10, command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="确定", width=10, command=self._ok).pack(side="right", padx=(0, 8))

        self.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self.destroy())
        self._place_near_parent()
        text_entry.focus_set()
        self.grab_set()

    def _place_near_parent(self):
        """不指定位置的话，对话框会被系统扔到屏幕左上角，这里让它出现在主窗口中间偏上。"""
        self.update_idletasks()
        parent = self.master
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_reqwidth()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_reqheight()) // 3
        self.geometry("+%d+%d" % (max(0, x), max(0, y)))

    # -- 按键捕获 --
    def _start_capture(self):
        self._capturing = True
        self.capture_btn.config(text="请按键…(Esc 取消)")
        self.hook.capture = self._cap_queue.put
        self._poll_capture()

    def _stop_capture(self):
        self._capturing = False
        self.hook.capture = None
        self.capture_btn.config(text="按下按键…")

    def _poll_capture(self):
        if not self._alive:
            return
        got = None
        while True:
            try:
                got = self._cap_queue.get_nowait()
            except queue.Empty:
                break
        if got is not None:
            self._stop_capture()
            if got != 0x1B:  # Esc 表示取消本次捕获
                self.hotkey_var.set(winapi.vk_name(got))
            return
        self.after(POLL_MS, self._poll_capture)

    # -- 确定 --
    def _ok(self):
        vk = winapi.parse_hotkey(self.hotkey_var.get())
        if vk is None:
            messagebox.showwarning("热键无效", "请点击“按下按键…”捕获一个单键热键，不支持组合键。", parent=self)
            return
        hotkey = winapi.vk_name(vk)
        if self.conflict_check(hotkey):
            messagebox.showwarning("热键重复", "热键 %s 已经被其他行占用。" % hotkey, parent=self)
            return
        if vk in TYPING_KEYS and not messagebox.askyesno(
            "确认热键",
            "%s 是打字时常用的键。设为热键后它在所有程序里都会被本程序吞掉，"
            "正常打字时就打不出这个字符了。\n\n确定要用它当热键吗？" % hotkey,
            parent=self,
        ):
            return
        text = self.text_var.get()
        if not text:
            messagebox.showwarning("文本为空", "请输入要自动输入的文本。", parent=self)
            return
        self.result = {"hotkey": hotkey, "text": text}
        self.destroy()

    def destroy(self):
        self._alive = False
        if self._capturing:
            self._stop_capture()
        super().destroy()


# ---------------------------------------------------------------- 主窗口
class App:
    def __init__(self, root, silent=False):
        self.root = root
        log("启动%s，启动时前台窗口是「%s」" % (
            "（--tray）" if silent else "",
            winapi.window_title(winapi.foreground_window()) or "（无）"))
        self.close_action, self.items, self.config_warning = load_config()
        self.hook = None
        self.tray = None
        self.tray_ok = False  # 托盘图标建成功后置 True

        self._hotkey_queue = queue.Queue()
        self._hotkey_map = {}
        self._enabled = True

        # 摆位置之前先别显示，否则能看见窗口从系统给的默认位置跳到屏幕中间
        root.withdraw()
        self._build_ui()
        if silent:
            # 开机自启用：窗口一直藏着，只在托盘待命，热键照常工作。
            # 位置在 _build_ui 里已经量好摆好了，用户点托盘图标时就是居中的。
            log("窗口保持隐藏，只在托盘待命")
        else:
            root.deiconify()
            # deiconify 只是发出显示请求，推一把让窗口真正映射出来，
            # 后面装钩子、建托盘时窗口就是就位状态了
            root.update()
        self._refresh()

        try:
            self.hook = winapi.KeyboardHook(self._on_key)
            self.hook.start()
        except OSError as e:
            messagebox.showerror(APP_NAME, "键盘钩子安装失败：%s\n\n热键将无法生效。" % e)

        self.tray = winapi.TrayIcon(
            root, ICON_PATH, tooltip=APP_NAME,
            on_activate=self._show_window, on_menu=self._tray_menu,
        )
        self.tray_ok = self.tray.active
        if not self.tray_ok:
            # 没有托盘图标，窗口一旦收起来就再也叫不回来，所以强制降级成关闭即退出，
            # 并把那个菜单项置灰，免得用户以为还能最小化。只改运行时状态，
            # self.close_action 保持用户原来的选择，这样配置不会被写坏。
            self.close_menu.entryconfig(
                self.close_menu.index(TRAY_CLOSE_LABEL), state="disabled"
            )
            self.close_action_var.set(CLOSE_QUIT)
            self._status("托盘图标创建失败，关闭窗口时将直接退出")

        if self.config_warning:
            messagebox.showwarning(APP_NAME, self.config_warning, parent=root)

        root.protocol("WM_DELETE_WINDOW", self._on_close_request)
        self.root.after(POLL_MS, self._poll_hotkeys)

    # -- 界面 --
    def _build_ui(self):
        root = self.root
        root.title(APP_NAME)
        root.geometry("660x420")
        root.minsize(480, 300)
        root.option_add("*Font", FONT)
        try:
            root.iconbitmap(ICON_PATH)  # 窗口和任务栏图标
        except tk.TclError:
            pass  # 图标文件缺失或损坏时不影响使用

        style = ttk.Style()
        style.configure("Treeview", font=FONT, rowheight=22)
        style.configure("Treeview.Heading", font=FONT)

        menubar = tk.Menu(root)

        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label="打开配置文件位置", command=self._open_config_dir)
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self._quit)
        menubar.add_cascade(label="文件(F)", menu=file_menu)

        self.enabled_var = tk.BooleanVar(value=True)
        self.autostart_var = tk.BooleanVar(value=(autostart_state() == "on"))
        self.close_action_var = tk.StringVar(value=self.close_action)
        settings_menu = tk.Menu(menubar, tearoff=False)
        settings_menu.add_checkbutton(
            label="启用热键", variable=self.enabled_var, command=self._toggle_enabled
        )
        settings_menu.add_checkbutton(
            label="开机自启", variable=self.autostart_var, command=self._toggle_autostart
        )
        settings_menu.configure(postcommand=self._refresh_autostart)
        close_menu = tk.Menu(settings_menu, tearoff=False)
        close_menu.add_radiobutton(
            label=TRAY_CLOSE_LABEL, variable=self.close_action_var,
            value=CLOSE_TRAY, command=self._set_close_action,
        )
        close_menu.add_radiobutton(
            label="直接退出程序", variable=self.close_action_var,
            value=CLOSE_QUIT, command=self._set_close_action,
        )
        settings_menu.add_cascade(label="关闭窗口时", menu=close_menu)
        self.close_menu = close_menu
        menubar.add_cascade(label="设置(S)", menu=settings_menu)
        self.settings_menu = settings_menu

        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(label="使用说明", command=self._show_help)
        help_menu.add_command(label="关于", command=self._show_about)
        menubar.add_cascade(label="帮助(H)", menu=help_menu)

        root.config(menu=menubar)

        table = ttk.Frame(root)
        table.pack(fill="both", expand=True, padx=8, pady=(8, 0))

        self.tree = ttk.Treeview(
            table, columns=("no", "hotkey", "text"), show="headings", selectmode="browse"
        )
        self.tree.heading("no", text="序号")
        self.tree.heading("hotkey", text="热键")
        self.tree.heading("text", text="文本")
        self.tree.column("no", width=60, anchor="center", stretch=False)
        self.tree.column("hotkey", width=100, anchor="center", stretch=False)
        self.tree.column("text", width=440, anchor="w")

        vsb = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(table, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        self.tree.bind("<Double-1>", self._on_double_click)

        actions = ttk.Frame(root)
        actions.pack(fill="x", padx=8, pady=8)
        ttk.Button(actions, text="添加", width=10, command=self._on_add).pack(side="left")
        ttk.Button(actions, text="删除", width=10, command=self._on_delete).pack(side="left", padx=6)
        ttk.Button(actions, text="修改", width=10, command=self._on_edit).pack(side="left")
        ttk.Label(actions, text=HINT, foreground="#333333").pack(side="left", padx=(16, 0))

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(root, textvariable=self.status_var, anchor="w", relief="sunken", padding=(6, 3)).pack(
            fill="x", side="bottom"
        )

        self._center_on_screen()

    def _center_on_screen(self):
        """把窗口摆到主屏正中。

        两点讲究：一是按工作区而不是整块屏幕算，否则会被底部的任务栏顶得偏下；
        二是 wm geometry 的 +x+y 定位的是窗口外框，而 WxH 是客户区尺寸，两个坐标系
        不一样，所以要先把边框和标题栏量出来补掉，直接拿客户区尺寸算会偏几十像素。
        得等控件和菜单都建完，量到的装饰高度才是最终值。
        """
        root = self.root
        # 窗口还没映射时 winfo_rootx/y 给的是没意义的数字，得先摆到一个确定的位置，
        # 量出来的客户区左上角和窗口左上角之差才是真正的边框、标题栏宽度。
        root.geometry("+0+0")
        root.update_idletasks()
        border = root.winfo_rootx() - root.winfo_x()   # 左右边框宽度
        caption = root.winfo_rooty() - root.winfo_y()  # 标题栏 + 菜单栏高度
        outer_w = root.winfo_width() + 2 * border
        outer_h = root.winfo_height() + caption + border
        left, top, right, bottom = winapi.work_area()
        # 屏幕小到装不下窗口时（例如 1080p 开了 300% 缩放），居中的坐标会是负的，
        # 窗口就跑到了屏幕外面、标题栏也够不着。这种情况退回贴着左上角。
        root.geometry("+%d+%d" % (max(left, (left + right - outer_w) // 2),
                                  max(top, (top + bottom - outer_h) // 2)))

    # -- 表格 --
    def _refresh(self):
        self.tree.delete(*self.tree.get_children())
        for index, item in enumerate(self.items):
            self.tree.insert(
                "", "end", iid=str(index), values=(index + 1, item["hotkey"], item["text"])
            )
        self._rebuild_hotkey_map()

    def _rebuild_hotkey_map(self):
        mapping = {}
        for item in self.items:
            vk = winapi.parse_hotkey(item["hotkey"])
            if vk is not None:
                mapping[vk] = item["text"]
        self._hotkey_map = mapping

    def _selected_index(self):
        selection = self.tree.selection()
        return int(selection[0]) if selection else None

    def _conflict(self, hotkey, exclude=None):
        return any(
            item["hotkey"].upper() == hotkey.upper() and index != exclude
            for index, item in enumerate(self.items)
        )

    def _save(self):
        try:
            save_config(self.close_action, self.items)
        except OSError as e:
            messagebox.showwarning("保存失败", "无法写入配置文件：\n%s" % e, parent=self.root)

    # -- 增删改 --
    def _on_add(self):
        dialog = ItemDialog(self.root, self.hook, "添加", self._conflict)
        self.root.wait_window(dialog)
        if dialog.result:
            self.items.append(dialog.result)
            self._refresh()
            self._save()

    def _on_edit(self):
        index = self._selected_index()
        if index is None:
            messagebox.showinfo(APP_NAME, "请先选中一行。", parent=self.root)
            return
        current = self.items[index]
        dialog = ItemDialog(
            self.root, self.hook, "修改",
            lambda hotkey: self._conflict(hotkey, exclude=index),
            hotkey=current["hotkey"], text=current["text"],
        )
        self.root.wait_window(dialog)
        if dialog.result:
            self.items[index] = dialog.result
            self._refresh()
            self._save()

    def _on_delete(self):
        index = self._selected_index()
        if index is None:
            messagebox.showinfo(APP_NAME, "请先选中一行。", parent=self.root)
            return
        hotkey = self.items[index]["hotkey"]
        if not messagebox.askyesno(APP_NAME, "确定删除热键 %s 这一行吗？" % hotkey, parent=self.root):
            return
        del self.items[index]
        self._refresh()
        self._save()
        self._status("已删除 %s" % hotkey)

    def _on_double_click(self, _event):
        index = self._selected_index()
        if index is None:
            return
        text = self.items[index]["text"]
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._status("已复制到剪贴板（%d 个字符）" % len(text))

    # -- 热键触发（钩子线程 -> 主线程）--
    def _on_key(self, vk, is_new_press):
        """在钩子线程中执行，只做查表和入队，绝不能触碰界面。"""
        if not self._enabled:
            return False
        text = self._hotkey_map.get(vk)
        if text is None:
            return False
        if is_new_press:
            self._hotkey_queue.put(text)
        return True  # 吞掉这个键，避免同时传到当前程序

    def _poll_hotkeys(self):
        if self._consume_show_request():
            self._show_window()
        while True:
            try:
                text = self._hotkey_queue.get_nowait()
            except queue.Empty:
                break
            self._inject(text)
        self.root.after(POLL_MS, self._poll_hotkeys)

    def _consume_show_request(self):
        """吃掉别的实例留下的「亮窗」信号（见 request_show）。"""
        if not os.path.exists(SHOW_FLAG):
            return False
        try:
            os.remove(SHOW_FLAG)
        except OSError:
            pass
        return True

    # -- 文本注入：放进剪贴板再发 Ctrl+V --
    def _inject(self, text):
        target = winapi.foreground_window()
        title = winapi.window_title(target) or "当前窗口"

        previous = winapi.get_clipboard_text()
        if not winapi.set_clipboard_text(text):
            self._status("写入剪贴板失败，可能有其他程序正占着剪贴板")
            return
        if not winapi.send_paste():
            # 被管理员权限的目标窗口拦下时（UIPI），SendInput 只静默返回 0，
            # 错误码不可靠（文档明说看不出来），只能自己去查对方属主进程的令牌
            if winapi.window_elevated(target) and not winapi.is_elevated():
                self._status("目标窗口是管理员权限，粘贴被系统拦下。"
                             "托盘右键「以管理员身份重启」后再试")
            else:
                self._status("粘贴失败：%s" % winapi.last_error_message())
            return

        self._status("已向「%s」粘贴 %d 个字符" % (title, len(text)))
        if previous is not None:
            # 目标程序是异步读剪贴板的，等它读完再把原来的内容放回去
            self.root.after(
                CLIPBOARD_RESTORE_MS,
                lambda: self._restore_clipboard(previous, text),
            )

    def _restore_clipboard(self, previous, mine):
        # 只有当剪贴板里还留着我们刚放的内容时才恢复，避免覆盖用户这期间新复制的东西
        if winapi.get_clipboard_text() == mine:
            winapi.set_clipboard_text(previous)

    # -- 菜单项 --
    def _toggle_enabled(self):
        self._enabled = self.enabled_var.get()
        self._status("热键已启用" if self._enabled else "热键已暂停")

    def _refresh_autostart(self):
        """菜单每次拉开时重问一遍，免得显示的还是启动那会儿的旧状态。"""
        self.autostart_var.set(autostart_state())

    def _toggle_autostart(self):
        want = self.autostart_var.get()
        if not winapi.is_elevated():
            # 建和删最高权限的任务都得是管理员。登录自启用上之后，程序开机起
            # 就是提权的，日常感觉不到这条限制；只有头一回开（或提权状态下退出
            # 之后又想关）会走到这里。
            messagebox.showwarning(
                "开机自启",
                "开机自启用的是一个最高权限的登录计划任务，要以管理员身份运行才能建或删。\n\n"
                "请先从托盘右键「以管理员身份重启」，再回来动这个开关。",
                parent=self.root)
            self.autostart_var.set(not want)
            return
        ok, out = set_autostart(want)
        if not ok:
            messagebox.showerror("开机自启", "改不了开机自启：\n%s" % out, parent=self.root)
            self.autostart_var.set(not want)
            return
        self._status("开机自启已打开" if want else "开机自启已关闭")

    def _open_config_dir(self):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        os.startfile(CONFIG_DIR)

    def _show_help(self):
        messagebox.showinfo(
            "使用说明",
            "1. 点“添加”，在弹出的窗口里点“按下按键…”，按下想用的热键（如 F2），再填文本。\n"
            "2. 之后在任何输入框里按该热键，文本会被粘贴进去。\n"
            "3. 粘贴会临时占用剪贴板，输入完会自动把原来的内容还原。\n"
            "4. 被设为热键的按键不会再传给其他程序。把字母、数字这类打字常用的键设为热键时"
            "会先弹窗确认——它们会被全局吞掉，正常打字就打不出这个字符了。\n"
            "5. 如果目标程序以管理员身份运行，本程序也需提权（托盘右键「以管理员身份重启」），否则无法输入。\n"
            "6. 关闭窗口默认最小化到托盘，程序继续在后台响应热键。双击托盘图标可以重新打开窗口，"
            "右键托盘图标可以退出；想改成关闭即退出，见“设置 → 关闭窗口时”。\n"
            "7. 需要临时让出这些按键时，在“设置”菜单里取消勾选“启用热键”。",
            parent=self.root,
        )

    def _show_about(self):
        privilege = "管理员" if winapi.is_elevated() else "普通"
        messagebox.showinfo(
            APP_NAME,
            "版本号：%s\n当前权限：%s\n\n配置路径：%s" % (VERSION, privilege, CONFIG_PATH),
            parent=self.root,
        )

    # -- 关闭窗口 / 托盘 --
    def _set_close_action(self):
        self.close_action = self.close_action_var.get()
        self._save()
        self._status(
            "关闭窗口时将最小化到托盘" if self.close_action == CLOSE_TRAY
            else "关闭窗口时将直接退出"
        )

    def _on_close_request(self):
        if self.close_action == CLOSE_TRAY and self.tray_ok:
            self._hide_to_tray()
        else:
            self._quit()

    def _hide_to_tray(self):
        self.root.withdraw()
        self._status("已最小化到托盘，双击托盘图标可以重新打开")

    def _show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        # 唤出时进程多半在后台（托盘里被双击、或吃了别的实例留的信号），
        # Tk 自己的 focus 未必拿得到前台，补一手。提权后的进程挂得到低权限
        # 线程，AttachThreadInput 这条路走得通
        hwnd = winapi.user32.GetAncestor(self.root.winfo_id(), winapi.GA_ROOT)
        if winapi.foreground_window() != hwnd:
            winapi.force_foreground_window(hwnd)

    def _tray_menu(self):
        choice = winapi.show_popup_menu(self.tray.hwnd, self._tray_items())
        if choice == TRAY_SHOW:
            self._show_window()
        elif choice == TRAY_ELEVATE:
            self._relaunch_elevated()
        elif choice == TRAY_QUIT:
            self._quit()

    def _tray_items(self):
        items = [(TRAY_SHOW, "显示主窗口")]
        # 已经是管理员权限就不再显示提权项，免得用户以为还没提
        if not winapi.is_elevated():
            items.append((TRAY_ELEVATE, "以管理员身份重启"))
        items.append((TRAY_QUIT, "退出"))
        return items

    def _relaunch_elevated(self):
        """以管理员身份重启本程序。

        管理员权限平时用不着，只有往管理员权限的窗口里粘贴时才需要（UIPI 限制），
        所以默认普通权限启动，需要时从这里提权一次——从 UAC 弹窗点「是」本身就是
        用户亲手同意的，新实例正常拿前台，不会再有强抢前台那类毛病。
        新实例带 --restart，会等本实例退出、互斥体放开后才起来，两边不会撞车。
        """
        if getattr(sys, "frozen", False):
            exe, params = sys.executable, "--restart"
        else:
            exe = sys.executable
            params = '"%s" --restart' % os.path.abspath(__file__)
        if not winapi.shell_runas(exe, params):
            self._status("已取消提权，继续以普通权限运行")  # UAC 上点了「否」
            return
        self._quit()  # 用户已同意，退出给提权实例让位

    def _quit(self):
        log("退出")
        if self.tray:
            self.tray.close()
        if self.hook:
            self.hook.stop()
        self.root.destroy()

    def _status(self, message):
        self.status_var.set(message)


def main():
    # --tray：开机自启（注册表 Run 键）用的，启动后不弹窗口，安静地待在托盘里
    silent = "--tray" in sys.argv
    # 两个实例会各装一个键盘钩子互相抢按键，所以只允许跑一个。
    # --restart：提权重启来的，老实例退出、互斥体放开要一小会儿，得等它
    if "--restart" in sys.argv:
        mutex = winapi.acquire_single_instance_with_wait(MUTEX_NAME, 10)
    else:
        mutex = winapi.acquire_single_instance(MUTEX_NAME)
    if mutex is None:
        # 已有实例多半是提权后待在托盘里的。普通权限的我们没法直接去操作
        # 管理员实例的窗口（UIPI 连这条路也拦），留个信号让它自己亮出来。
        log("已经有实例在跑，留信号叫它把窗口亮出来，本次不再启动")
        request_show()
        return 1
    root = tk.Tk()
    App(root, silent=silent)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
