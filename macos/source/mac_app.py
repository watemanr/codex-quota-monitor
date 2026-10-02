"""macOS entry point. Shared collection/timer code lives in windows/source."""
import argparse
import json
import os
from pathlib import Path
import sys

SHARED=Path(__file__).resolve().parents[2]/'windows'/'source'
if SHARED.is_dir():sys.path.insert(0,str(SHARED))
from collector_core import atomic_json, collect_snapshot, default_home, error_text


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--data-dir',type=Path)
    parser.add_argument('--collect-only',action='store_true')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--account',default='my-codex')
    parser.add_argument('--codex-home',type=Path,default=default_home())
    parser.add_argument('--smoke-test',action='store_true',help='离线检查菜单栏与窗口，不读取账号或上传')
    args=parser.parse_args()
    if sys.platform!='darwin':parser.error('此入口需要 macOS；Windows 请使用 Windows 版')
    os.umask(0o077)
    if args.collect_only:
        if not args.output:parser.error('--collect-only 需要 --output')
        try:atomic_json(args.output,collect_snapshot(args.codex_home,args.account,True))
        except Exception as exc:
            atomic_json(args.output,{'error':error_text(exc)});return 1
        return 0
    import tkinter as tk
    from tkinter import messagebox
    from mac_settings import MacSettingsStore
    from mac_tray import MacStatusTray
    from windows_app import CollectorApp
    data=args.data_dir or Path.home()/'Library'/'Application Support'/'CodexUsageCollector'
    if args.smoke_test and not args.data_dir:parser.error('--smoke-test 需要独立的 --data-dir 测试目录')
    data.mkdir(parents=True,exist_ok=True);data.chmod(0o700)
    root=tk.Tk()
    try:
        app=CollectorApp(root,data,settings_store=MacSettingsStore(),tray_factory=MacStatusTray,platform_name='darwin')
        if args.smoke_test:
            def finish():
                ok=bool(app.tray and app.tray.visible)
                atomic_json(data/'smoke-result.json',{'window_created':True,'menu_bar_created':ok,'account_read':False,'uploaded':False})
                app.close(force=True)
            root.after(1200,finish)
        root.mainloop()
    except Exception as exc:
        if args.smoke_test:
            atomic_json(data/'smoke-result.json',{'window_created':False,'menu_bar_created':False,'error':error_text(exc)})
        else:messagebox.showerror('启动失败',error_text(exc),parent=root)
        root.destroy();return 1
    return 0

if __name__=='__main__':raise SystemExit(main())
