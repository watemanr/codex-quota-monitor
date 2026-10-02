"""Windows notification-area icon; callbacks are delivered to Tk through a queue.
API reference: https://learn.microsoft.com/windows/win32/api/shellapi/ns-shellapi-notifyicondataw
"""
import ctypes as c
from ctypes import wintypes as w
import os
import uuid
from quota_display import quota_tooltip


class GUID(c.Structure):
    _fields_=[('Data1',w.DWORD),('Data2',w.WORD),('Data3',w.WORD),('Data4',w.BYTE*8)]


class NOTIFYICONDATA(c.Structure):
    _fields_=[('cbSize',w.DWORD),('hWnd',w.HWND),('uID',w.UINT),('uFlags',w.UINT),
              ('uCallbackMessage',w.UINT),('hIcon',w.HICON),('szTip',w.WCHAR*128),
              ('dwState',w.DWORD),('dwStateMask',w.DWORD),('szInfo',w.WCHAR*256),
              ('uTimeout',w.UINT),('szInfoTitle',w.WCHAR*64),('dwInfoFlags',w.DWORD),
              ('guidItem',GUID),('hBalloonIcon',w.HICON)]



def truncate_tooltip(text):
    # szTip holds 128 UTF-16 code units including the terminator.
    return str(text).replace('\0','').encode('utf-16-le',errors='replace')[:254].decode('utf-16-le',errors='ignore')


class WindowsTray:
    CALLBACK=0x8001
    COMMANDS={1:'show',2:'hide',3:'toggle_auto',4:'exit'}

    def __init__(self,icon_path,callback):
        if os.name!='nt':raise OSError('托盘仅支持 Windows')
        self.callback=callback
        self.visible=False
        self.tooltip=quota_tooltip(None)
        self._applied_tooltip=None
        self.hwnd=None
        self.icon=None
        self.user=c.WinDLL('user32',use_last_error=True)
        self.shell=c.WinDLL('shell32',use_last_error=True)
        self.kernel=c.WinDLL('kernel32',use_last_error=True)
        self.proc_type=c.WINFUNCTYPE(c.c_ssize_t,w.HWND,w.UINT,w.WPARAM,w.LPARAM)
        class WNDCLASS(c.Structure):
            _fields_=[('style',w.UINT),('lpfnWndProc',self.proc_type),('cbClsExtra',c.c_int),('cbWndExtra',c.c_int),
                ('hInstance',w.HINSTANCE),('hIcon',w.HICON),('hCursor',w.HANDLE),('hbrBackground',w.HBRUSH),
                ('lpszMenuName',w.LPCWSTR),('lpszClassName',w.LPCWSTR)]
        self.WNDCLASS=WNDCLASS
        def api(lib,name,args,restype):
            fn=getattr(lib,name);fn.argtypes=args;fn.restype=restype;return fn
        api(self.kernel,'GetModuleHandleW',[w.LPCWSTR],w.HMODULE)
        api(self.user,'RegisterClassW',[c.POINTER(WNDCLASS)],w.ATOM)
        api(self.user,'UnregisterClassW',[w.LPCWSTR,w.HINSTANCE],w.BOOL)
        api(self.user,'CreateWindowExW',[w.DWORD,w.LPCWSTR,w.LPCWSTR,w.DWORD,c.c_int,c.c_int,c.c_int,c.c_int,w.HWND,w.HMENU,w.HINSTANCE,w.LPVOID],w.HWND)
        api(self.user,'DefWindowProcW',[w.HWND,w.UINT,w.WPARAM,w.LPARAM],c.c_ssize_t)
        api(self.user,'DestroyWindow',[w.HWND],w.BOOL)
        api(self.user,'RegisterWindowMessageW',[w.LPCWSTR],w.UINT)
        api(self.user,'LoadImageW',[w.HINSTANCE,w.LPCWSTR,w.UINT,c.c_int,c.c_int,w.UINT],w.HANDLE)
        api(self.user,'DestroyIcon',[w.HICON],w.BOOL)
        api(self.user,'CreatePopupMenu',[],w.HMENU)
        api(self.user,'AppendMenuW',[w.HMENU,w.UINT,c.c_size_t,w.LPCWSTR],w.BOOL)
        api(self.user,'DestroyMenu',[w.HMENU],w.BOOL)
        api(self.user,'GetCursorPos',[c.POINTER(w.POINT)],w.BOOL)
        api(self.user,'SetForegroundWindow',[w.HWND],w.BOOL)
        api(self.user,'TrackPopupMenu',[w.HMENU,w.UINT,c.c_int,c.c_int,c.c_int,w.HWND,w.LPVOID],w.UINT)
        api(self.user,'PostMessageW',[w.HWND,w.UINT,w.WPARAM,w.LPARAM],w.BOOL)
        api(self.shell,'Shell_NotifyIconW',[w.DWORD,c.POINTER(NOTIFYICONDATA)],w.BOOL)
        self.instance=self.kernel.GetModuleHandleW(None)
        self.class_name='CodexUsageTray-'+uuid.uuid4().hex
        self.taskbar_message=self.user.RegisterWindowMessageW('TaskbarCreated')
        self.proc=self.proc_type(self._window_proc)
        wc=WNDCLASS();wc.lpfnWndProc=self.proc;wc.hInstance=self.instance;wc.lpszClassName=self.class_name
        if not self.user.RegisterClassW(c.byref(wc)):raise c.WinError(c.get_last_error())
        try:
            self.hwnd=self.user.CreateWindowExW(0,self.class_name,'CodexUsageCollectorTray',0,0,0,0,0,None,None,self.instance,None)
            if not self.hwnd:raise c.WinError(c.get_last_error())
            self.icon=self.user.LoadImageW(None,str(icon_path),1,32,32,0x10)
            if not self.icon:raise c.WinError(c.get_last_error())
        except Exception:
            self.close();raise

    def data(self):
        data=NOTIFYICONDATA();data.cbSize=c.sizeof(data);data.hWnd=self.hwnd;data.uID=1
        return data

    def show(self):
        if self.visible:return True
        data=self.data();data.uFlags=1|2|4;data.uCallbackMessage=self.CALLBACK
        data.hIcon=self.icon;data.szTip=self.tooltip
        self.visible=bool(self.shell.Shell_NotifyIconW(0,c.byref(data)))
        self._applied_tooltip=self.tooltip if self.visible else None
        return self.visible

    def set_tooltip(self,text):
        self.tooltip=truncate_tooltip(text)
        if not self.visible:return True
        if self._applied_tooltip==self.tooltip:return True
        data=self.data();data.uFlags=4;data.szTip=self.tooltip
        accepted=bool(self.shell.Shell_NotifyIconW(1,c.byref(data)))
        if accepted:self._applied_tooltip=self.tooltip
        return accepted

    def hide(self):
        if self.visible:
            self.shell.Shell_NotifyIconW(2,c.byref(self.data()))
            self.visible=False

    def notify(self,title,message):
        if not self.visible:return False
        data=self.data();data.uFlags=0x10;data.szInfoTitle=title[:63];data.szInfo=message[:255]
        data.dwInfoFlags=1|0x80  # Informational, respects Windows quiet time.
        return bool(self.shell.Shell_NotifyIconW(1,c.byref(data)))

    def _window_proc(self,hwnd,message,wp,lp):
        try:
            if message==self.taskbar_message and self.visible:
                self.visible=False
                if not self.show():self.callback('tray_failed')
                return 0
            if message==self.CALLBACK:
                action=lp & 0xffff
                if action in (0x202,0x203,0x405):self.callback('show')
                elif action in (0x205,0x7b):self.popup(hwnd)
                return 0
            if message==0x111:
                command=self.COMMANDS.get(wp & 0xffff)
                if command:self.callback(command)
                return 0
            if message==0x10:
                self.callback('exit');return 0
        except Exception:
            self.callback('tray_failed')
        return self.user.DefWindowProcW(hwnd,message,wp,lp)

    def popup(self,hwnd):
        menu=self.user.CreatePopupMenu()
        try:
            for command,title in ((1,'打开主界面'),(2,'隐藏到托盘'),(3,'开始 / 停止自动运行'),(4,'退出程序')):
                self.user.AppendMenuW(menu,0,command,title)
            point=w.POINT();self.user.GetCursorPos(c.byref(point));self.user.SetForegroundWindow(hwnd)
            chosen=self.user.TrackPopupMenu(menu,0x100|0x2,point.x,point.y,0,hwnd,None)
            self.user.PostMessageW(hwnd,0,0,0)
            if chosen in self.COMMANDS:self.callback(self.COMMANDS[chosen])
        finally:self.user.DestroyMenu(menu)

    def close(self):
        self.hide()
        if self.hwnd:self.user.DestroyWindow(self.hwnd);self.hwnd=None
        if self.icon:self.user.DestroyIcon(self.icon);self.icon=None
        self.user.UnregisterClassW(self.class_name,self.instance)
