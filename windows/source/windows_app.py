"""Shared desktop GUI with injectable settings and notification-area backends."""
import argparse
import ctypes
import datetime as dt
import json
import os
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import webbrowser
import urllib.parse

from collector_core import (VERSION, CollectorError, atomic_json, default_home, normalize_url,
    validate_account, collect_snapshot, upload_snapshot, error_text, load_settings, save_settings, http_json)


from automatic import IntervalSchedule, Outbox, flush_outbox, interval_minutes
from usage_monitor import UsageMonitor, notification_text
from quota_display import quota_tooltip
if os.name=='nt':
    from windows_tray import WindowsTray
else:
    WindowsTray=None


class CollectorApp:
    def __init__(self, root, data_dir, settings_store=None, tray_factory=None, platform_name=None):
        self.root=root
        self.data_dir=Path(data_dir)
        self.config_path=self.data_dir/'settings.json'
        self.settings_store=settings_store
        self.tray_factory=tray_factory or WindowsTray
        self.platform_name=platform_name or sys.platform
        self.font_family='Helvetica Neue' if self.platform_name=='darwin' else 'Microsoft YaHei UI'
        self.secret_label='macOS 钥匙串' if self.platform_name=='darwin' else 'Windows 加密'
        self.load_preferences=settings_store.load if settings_store else load_settings
        self.save_settings=settings_store.save if settings_store else save_settings
        self.config,warning=self.load_preferences(self.config_path)
        self.messages=queue.Queue()
        self.busy=False
        self.closed=False
        self.snapshot=None
        self.schedule=IntervalSchedule()
        self.auto_config=None
        self.job_auto=False
        self.last_timer_draw=0
        self.outbox=Outbox(self.data_dir/'pending.sqlite3')
        self.monitor=UsageMonitor(self.data_dir/'usage_monitor.sqlite3')
        self.tray=None
        self.popup_window=None
        self.popup_after=None
        self.background=tk.BooleanVar(value=self.config.get('background',True))
        self.notifications=tk.BooleanVar(value=self.config.get('notifications',True))
        self.reset_text={300:tk.StringVar(value='上次重置：尚未检测到'),10080:tk.StringVar(value='上次重置：尚未检测到')}
        self.config.setdefault('bindings',{})
        self.status=tk.StringVar(value='准备就绪')
        self.detail=tk.StringVar(value='先填写设置，或点击“仅采集”查看本机数据。')
        self.account=tk.StringVar(value=self.config.get('account','my-codex'))
        self.server=tk.StringVar(value=self.config.get('server',''))
        self.token=tk.StringVar(value=self.config.get('token',''))
        self.home=tk.StringVar(value=self.config.get('home',str(default_home())))
        self.remember=tk.BooleanVar(value=self.config.get('remember',True))
        self.analytics=tk.BooleanVar(value=self.config.get('analytics',True))
        self.show_token=tk.BooleanVar(value=False)
        self.read_enabled=tk.BooleanVar(value=self.config.get('read_enabled',True))
        self.upload_enabled=tk.BooleanVar(value=self.config.get('upload_enabled',True))
        self.read_minutes=tk.StringVar(value=str(self.config.get('read_minutes',5)))
        self.upload_minutes=tk.StringVar(value=str(self.config.get('upload_minutes',10)))
        self.auto_info=tk.StringVar(value='自动运行尚未启动')
        self.pending_info=tk.StringVar(value='待上传：0 条')
        self.weekly=tk.StringVar(value='—')
        self.five_hour=tk.StringVar(value='—')
        self.summary=tk.StringVar(value='尚未采集\n\n这里显示本次实际读取的额度；\n缺失窗口不会显示为 0。')
        root.title('Codex 用量采集器')
        self.build_menu()
        try:root.iconbitmap(str(Path(getattr(sys, '_MEIPASS', Path(__file__).parent))/'app.ico'))
        except tk.TclError:pass
        scale=max(0.75,min(root.winfo_fpixels('1i')/96,(root.winfo_screenwidth()-80)/1000,(root.winfo_screenheight()-100)/900))
        root.tk.call('tk','scaling',scale*96/72)
        root.geometry(f'{round(1000*scale)}x{round(900*scale)}')
        root.minsize(round(960*scale),round(850*scale))
        root.configure(bg='#f3f6fb')
        style=ttk.Style(root)
        style.theme_use('clam')
        style.configure('.',font=(self.font_family,10))
        style.configure('TFrame',background='#f3f6fb')
        style.configure('TLabel',background='#f3f6fb',foreground='#172b4d')
        style.configure('Muted.TLabel',foreground='#64748b')
        style.configure('TLabelframe',background='#f3f6fb',bordercolor='#dce3ef')
        style.configure('TLabelframe.Label',background='#f3f6fb',foreground='#172b4d',font=(self.font_family,11,'bold'))
        style.configure('TButton',padding=(12,8),background='#ffffff')
        style.configure('Primary.TButton',background='#2563eb',foreground='white',bordercolor='#2563eb')
        style.map('Primary.TButton',background=[('active','#1d4ed8'),('disabled','#adc3ed')],foreground=[('disabled','#f8fafc')])
        style.configure('TEntry',padding=7,fieldbackground='white')
        style.configure('TCheckbutton',background='#f3f6fb')
        style.configure('Big.TLabel',font=(self.font_family,28,'bold'),foreground='#2563eb')
        root.columnconfigure(0,weight=1)
        root.rowconfigure(5,weight=1)
        header=tk.Frame(root,bg='#122239',height=round(86*scale))
        header.grid(row=0,column=0,sticky='ew')
        header.grid_propagate(False)
        tk.Label(header,text='Codex 用量采集器',font=(self.font_family,20,'bold'),bg='#122239',fg='white').pack(anchor='w',padx=26,pady=(12,0))
        tk.Label(header,text='本机读取 · 主动上传 · 无需调用 AI',bg='#122239',fg='#b9c9df',font=(self.font_family,10)).pack(anchor='w',padx=28,pady=(2,0))
        body=ttk.Frame(root,padding=(24,18,24,0))
        body.grid(row=1,column=0,sticky='nsew')
        body.columnconfigure(0,weight=3)
        body.columnconfigure(1,weight=2)
        form=ttk.LabelFrame(body,text=' 连接设置 ',padding=16)
        form.grid(row=0,column=0,sticky='nsew',padx=(0,16))
        form.columnconfigure(1,weight=1)
        self.inputs=[]
        fields=[('网页账号名称',self.account),('服务地址',self.server),('上传密钥',self.token),('Codex 登录目录',self.home)]
        for row,(label,var) in enumerate(fields):
            ttk.Label(form,text=label).grid(row=row,column=0,sticky='w',padx=(0,10),pady=8)
            entry=ttk.Entry(form,textvariable=var,show='●' if var is self.token else '')
            entry.grid(row=row,column=1,columnspan=1 if var is self.home else 2,sticky='ew',pady=8)
            self.inputs.append(entry)
            if var is self.token:
                self.token_entry=entry
        self.browse=ttk.Button(form,text='选择目录',command=self.choose_home)
        self.browse.grid(row=3,column=2,sticky='e',padx=(6,0))
        self.inputs.append(self.browse)
        toggles=ttk.Frame(form)
        toggles.grid(row=4,column=0,columnspan=3,sticky='ew',pady=(1,3))
        self.remember_check=ttk.Checkbutton(toggles,text='记住密钥（'+self.secret_label+'）',variable=self.remember)
        self.remember_check.pack(side='left')
        ttk.Checkbutton(toggles,text='显示',variable=self.show_token,command=lambda:self.token_entry.configure(show='' if self.show_token.get() else '●')).pack(side='right')
        self.analytics_check=ttk.Checkbutton(form,text='包含 30 天分析与聊天排行（含标题）',variable=self.analytics)
        self.analytics_check.grid(row=5,column=0,columnspan=3,sticky='w',pady=(3,5))
        self.inputs.extend([self.remember_check,self.analytics_check])
        ttk.Label(form,text='填写网页根地址；只采集时可留空。\n上传密钥使用 UPLOAD_TOKEN，不是 OpenAI API Key。',style='Muted.TLabel',font=(self.font_family,9)).grid(row=6,column=0,columnspan=3,sticky='w',pady=(4,0))
        panel=ttk.LabelFrame(body,text=' 本次读取 ',padding=18)
        panel.grid(row=0,column=1,sticky='nsew')
        ttk.Label(panel,text='5 小时 · 剩余额度',style='Muted.TLabel').pack(anchor='w')
        ttk.Label(panel,textvariable=self.five_hour,style='Big.TLabel').pack(anchor='w',pady=(2,4))
        ttk.Label(panel,textvariable=self.reset_text[300],wraplength=round(340*scale),justify='left',style='Muted.TLabel',font=(self.font_family,9)).pack(anchor='w',pady=(0,8))
        ttk.Separator(panel).pack(fill='x',pady=(1,14))
        ttk.Label(panel,text='每周 · 剩余额度',style='Muted.TLabel').pack(anchor='w')
        ttk.Label(panel,textvariable=self.weekly,style='Big.TLabel').pack(anchor='w',pady=(2,4))
        ttk.Label(panel,textvariable=self.reset_text[10080],wraplength=round(340*scale),justify='left',style='Muted.TLabel',font=(self.font_family,9)).pack(anchor='w',pady=(0,8))
        ttk.Label(panel,textvariable=self.summary,wraplength=round(320*scale),justify='left',style='Muted.TLabel',font=(self.font_family,9)).pack(anchor='w',pady=(8,0))
        actions=ttk.Frame(root,padding=(24,16,24,0))
        actions.grid(row=2,column=0,sticky='ew')
        self.collect_button=ttk.Button(actions,text='采集并上传',style='Primary.TButton',command=lambda:self.start(True))
        self.collect_button.pack(side='left',padx=(0,8))
        self.local_button=ttk.Button(actions,text='仅采集',command=lambda:self.start(False))
        self.local_button.pack(side='left',padx=(0,8))
        self.retry_button=ttk.Button(actions,text='立即上传',command=self.retry,state='disabled')
        self.retry_button.pack(side='left',padx=(0,8))
        self.export_button=ttk.Button(actions,text='导出 JSON',command=self.export,state='disabled')
        self.export_button.pack(side='left')
        self.open_button=ttk.Button(actions,text='打开网页',command=self.open_site)
        self.open_button.pack(side='right')
        self.save_button=ttk.Button(actions,text='保存设置',command=self.save)
        self.save_button.pack(side='right',padx=(0,8))
        self.inputs.append(self.save_button)
        auto_frame=ttk.LabelFrame(root,text=' 自动运行（0 = 关闭对应功能） ',padding=(12,8))
        auto_frame.grid(row=3,column=0,sticky='ew',padx=24,pady=(12,0))
        auto_frame.columnconfigure(6,weight=1)
        read_check=ttk.Checkbutton(auto_frame,text='每隔',variable=self.read_enabled)
        read_check.grid(row=0,column=0,sticky='w')
        read_spin=ttk.Spinbox(auto_frame,from_=0,to=1440,width=5,textvariable=self.read_minutes)
        read_spin.grid(row=0,column=1,padx=(4,6))
        ttk.Label(auto_frame,text='分钟读取',font=(self.font_family,9)).grid(row=0,column=2,sticky='w',padx=(0,20))
        upload_check=ttk.Checkbutton(auto_frame,text='每隔',variable=self.upload_enabled)
        upload_check.grid(row=0,column=3,sticky='w')
        upload_spin=ttk.Spinbox(auto_frame,from_=0,to=1440,width=5,textvariable=self.upload_minutes)
        upload_spin.grid(row=0,column=4,padx=(4,6))
        ttk.Label(auto_frame,text='分钟上传',font=(self.font_family,9)).grid(row=0,column=5,sticky='w')
        self.auto_button=ttk.Button(auto_frame,text='开始自动',command=self.toggle_auto)
        self.auto_button.grid(row=0,column=7,sticky='e',padx=(10,0))
        self.inputs.extend([read_check,read_spin,upload_check,upload_spin])
        ttk.Label(auto_frame,textvariable=self.auto_info,style='Muted.TLabel',font=(self.font_family,9),wraplength=round(900*scale)).grid(row=1,column=0,columnspan=8,sticky='w',pady=(6,0))
        ttk.Label(auto_frame,textvariable=self.pending_info,style='Muted.TLabel',font=(self.font_family,9)).grid(row=2,column=0,columnspan=8,sticky='w',pady=(4,0))
        status_box=ttk.Frame(root,padding=(24,14,24,8))
        status_box.grid(row=4,column=0,sticky='ew')
        ttk.Label(status_box,textvariable=self.status,font=(self.font_family,12,'bold')).pack(anchor='w')
        ttk.Label(status_box,textvariable=self.detail,style='Muted.TLabel',wraplength=round(920*scale)).pack(anchor='w',pady=(3,6))
        self.progress=ttk.Progressbar(status_box,mode='indeterminate')
        self.progress.pack(fill='x')
        logframe=ttk.Frame(root,padding=(24,0,24,10))
        logframe.grid(row=5,column=0,sticky='nsew')
        logframe.rowconfigure(0,weight=1)
        logframe.columnconfigure(0,weight=1)
        self.logbox=tk.Text(logframe,height=6,wrap='word',bg='white',fg='#334155',relief='flat',padx=12,pady=10,state='disabled',font=(self.font_family,9))
        self.logbox.grid(row=0,column=0,sticky='nsew')
        scrollbar=ttk.Scrollbar(logframe,command=self.logbox.yview)
        scrollbar.grid(row=0,column=1,sticky='ns')
        self.logbox.configure(yscrollcommand=scrollbar.set)
        ttk.Label(root,text='v'+VERSION+'  ·  '+('菜单栏常驻' if self.platform_name=='darwin' else '托盘常驻')+'可在“设置”中开关；启用后关闭窗口仍运行，选择“退出程序”结束。',style='Muted.TLabel',font=(self.font_family,9)).grid(row=6,column=0,sticky='w',padx=24,pady=(0,12))
        try:
            cached=json.loads((self.data_dir/'last_collection.json').read_text(encoding='utf8'))
            if isinstance(cached.get('reports'),dict) and cached['reports']:
                self.snapshot=cached
                self.monitor.observe(cached)  # Recover baseline/history without replaying old notifications.
                self.render_snapshot(cached)
                self.log('已载入上次采集，待上传队列保持原时间与 ID。')
                self.retry_button.configure(state='normal')
                self.export_button.configure(state='normal')
        except (ValueError,OSError,AttributeError):
            pass
        try:
            migrated=self.outbox.migrate_legacy(self.snapshot,self.config.get('server',''))
            if migrated:self.log('已把旧版本的最近一次采集加入队列；服务端会自动去重。')
        except Exception as exc:self.log('旧采集恢复提示：'+error_text(exc))
        self.log('程序只查询用量，不调用模型、不兑换额度、不上传登录凭证或聊天正文。')
        if warning:
            self.log(warning)
        self.refresh_controls()
        root.protocol('WM_DELETE_WINDOW',self.close)
        if self.platform_name=='darwin':
            root.createcommand('tk::mac::Quit',lambda:self.close(force=True))
            root.createcommand('tk::mac::ReopenApplication',self.show_window)
        root.bind('<Unmap>',self.on_minimize,add='+')
        self.apply_background()
        self.account.trace_add('write',lambda *args:self.render_resets())
        self.render_resets()
        self.after_id=root.after(100,self.drain)

    def log(self,message):
        self.logbox.configure(state='normal')
        self.logbox.insert('end',dt.datetime.now().strftime('%H:%M:%S')+'  '+message+'\n')
        if int(self.logbox.index('end-1c').split('.')[0])>1000:
            self.logbox.delete('1.0','101.0')
        self.logbox.see('end')
        self.logbox.configure(state='disabled')

    def settings(self,upload=False):
        account=validate_account(self.account.get())
        server=self.server.get().strip()
        if server:
            server=normalize_url(server)
        if upload and not server:
            raise CollectorError('请先填写服务地址；只查看本机额度可点击“仅采集”')
        token=self.token.get().strip()
        if upload and not token:
            raise CollectorError('请填写上传密钥 UPLOAD_TOKEN')
        return dict(account=account,server=server,token=token,home=str(Path(self.home.get()).expanduser()),
            remember=self.remember.get(),analytics=self.analytics.get(),bindings=self.config.get('bindings',{}),
            background=self.background.get(),notifications=self.notifications.get(),
            read_enabled=self.read_enabled.get(),upload_enabled=self.upload_enabled.get(),
            read_minutes=interval_minutes(self.read_minutes.get()),upload_minutes=interval_minutes(self.upload_minutes.get()))

    def save(self):
        try:
            cfg=self.settings()
            self.save_settings(self.config_path,cfg)
            self.config=cfg
            self.log('设置已保存；'+('上传密钥已安全保存（'+self.secret_label+'）。' if cfg['remember'] and cfg['token'] else '未保存上传密钥。'))
        except Exception as exc:
            self.fail(exc)

    def fail(self,exc):
        self.status.set('未完成')
        self.detail.set(error_text(exc))
        self.log(error_text(exc))

    def choose_home(self):
        chosen=filedialog.askdirectory(title='选择包含 auth.json 的 Codex 目录',initialdir=self.home.get())
        if chosen:self.home.set(chosen)

    def open_site(self):
        try:webbrowser.open(normalize_url(self.server.get()))
        except Exception as exc:self.fail(exc)

    def refresh_controls(self):
        for widget in self.inputs:
            widget.configure(state='disabled' if self.busy or self.schedule.active else 'normal')
        for widget in (self.collect_button,self.local_button):
            widget.configure(state='disabled' if self.busy else 'normal')
        self.retry_button.configure(state='disabled' if self.busy else 'normal')
        self.export_button.configure(state='normal' if self.snapshot and not self.busy else 'disabled')
        self.auto_button.configure(text='停止自动' if self.schedule.active else '开始自动',
            state='normal' if self.schedule.active or not self.busy else 'disabled')

    def set_busy(self,value):
        self.busy=value
        self.refresh_controls()
        if value:self.progress.start(12)
        else:self.progress.stop()

    def launch(self,cfg,collect,upload,auto_kind=None):
        if self.busy:return
        self.job_auto=auto_kind is not None
        if auto_kind:self.schedule.begin(auto_kind)
        self.set_busy(True)
        self.status.set(('自动' if auto_kind else '')+('读取中' if collect else '上传队列中'))
        self.detail.set('数据先存入本机队列，收到服务器确认后才移出队列。')
        threading.Thread(target=self.worker,args=(cfg,collect,upload),daemon=True).start()

    def start(self,upload):
        if self.busy:return
        try:
            cfg=self.settings(upload)
            self.save_settings(self.config_path,cfg)
            self.config=cfg
            self.launch(cfg,True,upload)
        except Exception as exc:self.fail(exc)

    def retry(self):
        if self.busy:return
        try:
            cfg=self.settings(True)
            self.launch(cfg,False,True)
        except Exception as exc:self.fail(exc)

    def toggle_auto(self):
        if self.schedule.active:
            self.schedule.stop()
            self.auto_config=None
            self.log('自动运行已停止；当前操作会完成。' if self.busy else '自动运行已停止。')
            self.refresh_controls()
            self.draw_schedule()
            return
        if self.busy:return
        try:
            cfg=self.settings(self.upload_enabled.get() and interval_minutes(self.upload_minutes.get())>0)
            self.schedule.start(cfg['read_minutes'],cfg['upload_minutes'],cfg['read_enabled'],cfg['upload_enabled'])
            self.save_settings(self.config_path,cfg)
            self.config=cfg
            self.auto_config=cfg if self.schedule.active else None
            if self.schedule.active:
                self.log('自动运行已开始：'+('首次读取立即执行。' if 'read' in self.schedule.seconds else '自动读取已关闭。')+('上传按独立间隔执行。' if 'upload' in self.schedule.seconds else '自动上传已关闭。'))
            else:
                self.status.set('自动功能已关闭')
                self.detail.set('设置已保存，仍可手动采集和上传。')
                self.log('自动读取和自动上传均已关闭，手动操作仍可使用。')
            self.draw_schedule()
            self.refresh_controls()
            self.tick_schedule()
        except Exception as exc:
            self.schedule.stop()
            self.fail(exc)
            self.refresh_controls()

    def draw_schedule(self):
        now=self.schedule.clock()
        parts=[]
        for kind,label in (('read','读取'),('upload','上传')):
            if self.schedule.running==kind:
                state='进行中'
            elif not getattr(self,kind+'_enabled').get() or getattr(self,kind+'_minutes').get().strip()=='0':
                state='已关闭'
            elif not self.schedule.active:
                state='已停止'
            elif kind not in self.schedule.seconds:
                state='已关闭'
            else:
                seconds=max(0,self.schedule.next_at[kind]-now)
                when=dt.datetime.fromtimestamp(time.time()+seconds).strftime('%H:%M:%S')
                state='等待当前操作结束' if seconds==0 and self.busy else when
            parts.append('下次'+label+'：'+state)
        self.auto_info.set(' · '.join(parts))
        try:
            current,other=self.outbox.scope(self.account.get().strip(),self.server.get().strip().rstrip('/'))
            self.pending_info.set('待上传：'+str(current)+' 条（当前账号 / 地址）'+('；其他账号或地址：'+str(other)+' 条' if other else ''))
        except Exception:
            self.pending_info.set('待上传队列暂时无法读取')

    def tick_schedule(self):
        if self.schedule.active and not self.busy:
            action=self.schedule.due()
            if action:
                self.launch(self.auto_config,action=='read',action=='upload',action)
        now=time.monotonic()
        if now-self.last_timer_draw>=1:
            self.draw_schedule()
            self.last_timer_draw=now

    def worker(self,cfg,collect,upload):
        progress=lambda s:self.messages.put(('progress',s))
        snapshot=None
        try:
            if collect:
                self.outbox.check_capacity()
                snapshot=collect_snapshot(cfg['home'],cfg['account'],cfg['analytics'],
                    cfg['bindings'].get(cfg['account']),progress)
                self.outbox.add(snapshot,cfg['server'])
                try:
                    events=self.monitor.observe(snapshot)
                    if events:self.messages.put(('quota_events',events))
                except Exception as exc:snapshot['warnings'].append('本地重置检测失败：'+error_text(exc))
                try:atomic_json(self.data_dir/'last_collection.json',snapshot)
                except Exception as exc:snapshot['warnings'].append('预览缓存保存失败，但待上传队列已保存：'+error_text(exc))
                self.messages.put(('snapshot',snapshot))
            results=flush_outbox(self.outbox,cfg['account'],cfg['server'],cfg['token'],progress) if upload else None
            if upload and results is not None:
                try:
                    identity=snapshot['identity'] if snapshot else cfg['bindings'].get(cfg['account'])
                    if identity:
                        remote=http_json(cfg['server']+'/api/reset-status?account='+urllib.parse.quote(cfg['account'],safe=''),cfg['token'],timeout=12)
                        if remote.get('account')==cfg['account']:
                            self.monitor.merge_remote(cfg['account'],identity,remote.get('last_resets'))
                            self.messages.put(('reset_history',None))
                except Exception:
                    progress('网页重置历史暂未同步，本机重置记录仍保留。')
            self.messages.put(('done',(snapshot,results)))
        except Exception as exc:
            self.messages.put(('error',error_text(exc)))

    def finish_job(self):
        if self.job_auto:self.schedule.finish()
        self.job_auto=False
        self.set_busy(False)
        self.draw_schedule()

    def drain(self):
        try:
            while True:
                kind,value=self.messages.get_nowait()
                if kind=='tray':
                    self.handle_tray(value)
                    if self.closed:return
                elif kind=='quota_events':
                    self.announce_events(value)
                elif kind=='reset_history':
                    self.render_resets()
                elif kind=='progress':
                    self.detail.set(value)
                    self.log(value)
                elif kind=='snapshot':
                    self.snapshot=value
                    self.render_snapshot(value)
                    self.config['bindings'][value['account']]=value['identity']
                    try:self.save_settings(self.config_path,self.config)
                    except Exception as exc:self.log('账号绑定保存失败：'+error_text(exc))
                elif kind=='error':
                    self.finish_job()
                    self.status.set('本轮未完成')
                    self.detail.set(value+'；未确认的数据保留在本机队列。')
                    self.log(value)
                elif kind=='done':
                    self.finish_job()
                    snapshot,results=value
                    warnings=snapshot['warnings'] if snapshot else []
                    for warning in warnings:self.log('提示：'+warning)
                    if results is None:
                        self.status.set('读取完成' if not warnings else '部分读取完成')
                        self.detail.set('数据已加入本机队列，等待上传；也可点击“立即上传”。')
                    else:
                        good=not results['errors'] and not warnings and not results['stale']
                        self.status.set('本轮上传完成' if good and results['sent'] else '本轮没有待上传数据' if good and not results['remaining'] else '部分完成 / 请查看记录')
                        summary='服务器确认 '+str(results['sent'])+' 条；待上传 '+str(results['remaining'])+' 条'
                        if results['duplicate']:summary+='；其中 '+str(results['duplicate'])+' 条为安全重试'
                        if results['stale']:summary+='；'+str(results['stale'])+' 条旧采样未覆盖最新状态'
                        if results['other']:summary+='；另有其他账号 / 地址的记录保留在队列'
                        self.detail.set(summary)
                        self.log(summary)
                        for error in results['errors']:self.log('上传失败，已保留：'+error)
        except queue.Empty:
            pass
        self.tick_schedule()
        self.after_id=self.root.after(100,self.drain)

    def render_snapshot(self,snapshot):
        windows=snapshot['reports'].get('usage',{}).get('windows',[])
        lines=['账号：'+snapshot['account']]
        cards=snapshot['reports'].get('usage',{}).get('reset_cards')
        lines.append('重置卡：'+(str(cards['available_count'])+' 张' if cards is not None else '未返回（需新版采集器及接口支持）'))
        for minutes,target in ((300,self.five_hour),(10080,self.weekly)):
            window=next((w for w in windows if w['window_minutes']==minutes),None)
            target.set(f"{100-window['used_percent']:g}%" if window else '未返回')
            if window and window.get('resets_at'):
                label='5 小时' if minutes==300 else '每周'
                when=dt.datetime.fromtimestamp(window['resets_at']).strftime('%m/%d %H:%M')
                lines.append(label+'预计重置：'+when)
        try:when=dt.datetime.fromisoformat(snapshot['collected_at']).astimezone().strftime('%m/%d %H:%M:%S')
        except ValueError:when='未知'
        lines.append('采样：'+when+'（本机时间）')
        if snapshot['reports'].get('analytics'):
            sections=snapshot['reports']['analytics']['analytics']
            lines.append('分析：'+str(len(sections.get('chats',{}).get('rows',[])))+' 个聊天，'+str(len(sections))+' 个区块')
        self.summary.set('\n'.join(lines))
        self.update_tray_tooltip()
        self.render_resets()

    def export(self):
        if not self.snapshot:return
        folder=filedialog.askdirectory(title='选择 JSON 导出目录')
        if folder:
            try:
                stamp=dt.datetime.now().strftime('%Y%m%d-%H%M%S')
                for section,payload in self.snapshot['reports'].items():
                    atomic_json(Path(folder)/(section+'-'+stamp+'.json'),payload)
                self.log('已导出独立 JSON，可在网页“接入 / 上传”中粘贴。')
            except Exception as exc:self.fail(exc)

    def build_menu(self):
        menubar=tk.Menu(self.root)
        actions=tk.Menu(menubar,tearoff=False)
        actions.add_command(label='打开主界面',command=self.show_window)
        actions.add_command(label='隐藏到菜单栏' if self.platform_name=='darwin' else '隐藏到托盘',command=self.hide_window)
        actions.add_separator()
        actions.add_command(label='退出程序',command=lambda:self.close(force=True))
        menubar.add_cascade(label='主菜单',menu=actions)
        settings=tk.Menu(menubar,tearoff=False)
        settings.add_checkbutton(label=('菜单栏常驻' if self.platform_name=='darwin' else '托盘常驻')+'（关闭窗口后继续运行）',variable=self.background,command=self.change_background)
        settings.add_checkbutton(label='额度归零 / 重置通知',variable=self.notifications,command=self.change_notifications)
        menubar.add_cascade(label='设置',menu=settings)
        self.root.configure(menu=menubar)

    def save_preferences(self):
        cfg=dict(self.config,background=self.background.get(),notifications=self.notifications.get())
        self.save_settings(self.config_path,cfg)
        self.config=cfg
        if self.auto_config is not None:
            self.auto_config.update(background=cfg['background'],notifications=cfg['notifications'])

    def change_background(self):
        self.apply_background()
        try:self.save_preferences()
        except Exception as exc:self.fail(exc)

    def change_notifications(self):
        try:
            self.save_preferences()
            self.log('额度通知已'+('开启。' if self.notifications.get() else '关闭；仍记录重置历史。'))
        except Exception as exc:self.fail(exc)

    def apply_background(self):
        if self.background.get():
            try:
                if self.tray is None:
                    icon=Path(getattr(sys,'_MEIPASS',Path(__file__).parent))/'app.ico'
                    self.tray=self.tray_factory(icon,lambda action:self.messages.put(('tray',action)))
                self.update_tray_tooltip()
                if not self.tray.show():raise OSError('系统托盘暂不可用')
                self.log('托盘常驻已开启；关闭窗口后继续运行，主菜单 / 托盘菜单可退出。')
            except Exception:
                self.background.set(False)
                self.show_window()
                self.log('托盘初始化失败，已保留主窗口，可在设置中重新开启。')
        else:
            if self.tray:self.tray.hide()
            if self.root.state()=='withdrawn':self.show_window()
            self.log('托盘常驻已关闭；关闭窗口将退出程序。')

    def update_tray_tooltip(self):
        if self.tray:self.tray.set_tooltip(quota_tooltip(self.snapshot))

    def show_window(self):
        self.root.deiconify()
        self.root.lift()

    def hide_window(self):
        if self.background.get() and self.tray and self.tray.visible:
            self.root.withdraw()
        else:self.log('先在“设置”中开启托盘常驻。')

    def on_minimize(self,event):
        if event.widget is self.root and self.root.state()=='iconic':self.hide_window()

    def handle_tray(self,action):
        if isinstance(action,tuple) and action[0]=='notification_fallback':
            if self.notifications.get():self.show_notification_popup(action[1],action[2])
            return
        if action=='show':self.show_window()
        elif action=='hide':self.hide_window()
        elif action=='toggle_auto':self.toggle_auto()
        elif action=='exit':self.close(force=True)
        elif action=='tray_failed':
            self.background.set(False)
            if self.tray:self.tray.hide()
            self.show_window()
            self.log('托盘暂不可用，已恢复主窗口。')

    def announce_events(self,events):
        title,text=notification_text(events)
        self.log(text)
        self.render_resets()
        if not self.notifications.get():return
        if self.tray and self.tray.notify(title,text):return
        self.show_notification_popup(title,text)

    def show_notification_popup(self,title,text):
        # Non-blocking fallback when system notifications are unavailable.
        if self.popup_after:
            self.root.after_cancel(self.popup_after);self.popup_after=None
        if self.popup_window and self.popup_window.winfo_exists():self.popup_window.destroy()
        popup=tk.Toplevel(self.root);self.popup_window=popup
        popup.title(title);popup.attributes('-topmost',True);popup.resizable(False,False)
        ttk.Label(popup,text=text,wraplength=440,padding=18,justify='left').pack(fill='both')
        ttk.Button(popup,text='知道了',command=popup.destroy).pack(pady=(0,12))
        popup.update_idletasks()
        popup.geometry(f'+{max(0,popup.winfo_screenwidth()-popup.winfo_width()-30)}+{max(0,popup.winfo_screenheight()-popup.winfo_height()-90)}')
        self.popup_after=self.root.after(20000,lambda:popup.destroy() if popup.winfo_exists() else None)

    def render_resets(self):
        account=self.account.get().strip()
        identity=self.config.get('bindings',{}).get(account)
        if self.snapshot and self.snapshot['account']==account:identity=self.snapshot['identity']
        resets=self.monitor.latest(account,identity)
        for minutes,var in self.reset_text.items():
            candidates=[r for r in resets.values() if r['window_minutes']==minutes]
            r=max(candidates,key=lambda x:x['detected_at']) if candidates else None
            if r:
                when=dt.datetime.fromtimestamp(r['detected_at']).strftime('%Y-%m-%d %H:%M:%S')
                var.set('上次重置：'+r['window_type']+' · '+r['type_label']+'\n检测于 '+when)
            else:var.set('上次重置：尚未检测到')

    def close(self,force=False):
        if not force and self.background.get() and self.tray and self.tray.visible:
            self.hide_window();return
        if self.busy and not messagebox.askyesno('仍在运行','现在退出会中断本次操作，未确认的上传保留在队列。是否退出？'):
            return
        self.schedule.stop()
        if getattr(self,'after_id',None):self.root.after_cancel(self.after_id)
        if self.popup_after:self.root.after_cancel(self.popup_after)
        if self.tray:self.tray.close();self.tray=None
        self.closed=True
        self.root.destroy()



def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--data-dir',type=Path,help='自定义本机设置与最近一次采集的保存目录')
    parser.add_argument('--collect-only',action='store_true',help='无窗口采集，结果写入 --output，不上传')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--account',default='my-codex')
    parser.add_argument('--codex-home',type=Path,default=default_home())
    args=parser.parse_args()
    if args.collect_only:
        if args.output is None:parser.error('--collect-only 需要 --output')
        try:
            snapshot=collect_snapshot(args.codex_home,args.account,True)
            atomic_json(args.output,snapshot)
            return
        except Exception as exc:
            atomic_json(args.output,{'error':error_text(exc)})
            sys.exit(1)
    data_dir=args.data_dir or Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'CodexUsageCollector'
    if os.name=='nt':
        try:ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (OSError,AttributeError):pass
    root=tk.Tk()
    CollectorApp(root,data_dir)
    root.mainloop()


if __name__=='__main__':
    main()
