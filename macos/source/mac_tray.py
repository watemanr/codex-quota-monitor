"""Native NSStatusItem and UserNotifications, driven by Tk's Cocoa event loop."""
import threading
import uuid

_TARGET_CLASS=None

def menu_title(tooltip):
    first=str(tooltip).splitlines()[0] if tooltip else ''
    for part in first.split(' · '):
        if part.endswith('%/weekly'):return part.replace('/weekly',' W')
        if part.endswith('%/5h'):return part.replace('/5h',' H')
    return 'Q'

class MacStatusTray:
    def __init__(self,icon_path,callback):
        import AppKit
        import Foundation
        global _TARGET_CLASS
        self.kit=AppKit;self.foundation=Foundation;self.callback=callback
        self.visible=False;self.item=None;self.tooltip='Codex 用量采集器 · 尚未读取额度'
        self.center=None;self.closed=False;self.lock=threading.Lock()
        self.permission=None;self.requesting=False;self.pending=[];self.notifications=None
        if _TARGET_CLASS is None:
            class CodexQuotaMenuTarget(Foundation.NSObject):
                def selected_(self,sender):
                    self.action_callback(str(sender.representedObject()))
            _TARGET_CLASS=CodexQuotaMenuTarget
        self.target=_TARGET_CLASS.alloc().init();self.target.action_callback=callback
        # UNUserNotificationCenter requires an application bundle; source runs use the UI fallback.
        if Foundation.NSBundle.mainBundle().bundleIdentifier()=='xyz.814305.codex-quota-collector':
            try:
                import UserNotifications as un
                self.notifications=un;self.center=un.UNUserNotificationCenter.currentNotificationCenter()
            except Exception:self.center=None

    def show(self):
        if self.visible:return True
        k=self.kit
        self.item=k.NSStatusBar.systemStatusBar().statusItemWithLength_(k.NSVariableStatusItemLength)
        menu=k.NSMenu.alloc().initWithTitle_('Codex 用量采集器')
        for title,action in [('打开主界面','show'),('隐藏到菜单栏','hide'),('开始 / 停止自动运行','toggle_auto'),('退出程序','exit')]:
            item=k.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title,'selected:','')
            item.setRepresentedObject_(action);item.setTarget_(self.target);menu.addItem_(item)
        self.item.setMenu_(menu);self.visible=True;self.set_tooltip(self.tooltip)
        return True

    def set_tooltip(self,text):
        self.tooltip=str(text)
        if self.item:
            button=self.item.button();button.setTitle_(menu_title(self.tooltip));button.setToolTip_(self.tooltip)
        return True

    def hide(self):
        if self.item:self.kit.NSStatusBar.systemStatusBar().removeStatusItem_(self.item)
        self.item=None;self.visible=False

    def _fallback(self,title,text):
        if not self.closed:self.callback(('notification_fallback',title,text))

    def _send(self,title,text):
        if self.closed:return
        un=self.notifications
        content=un.UNMutableNotificationContent.alloc().init()
        content.setTitle_(title);content.setBody_(text)
        request=un.UNNotificationRequest.requestWithIdentifier_content_trigger_(str(uuid.uuid4()),content,None)
        def complete(error):
            if error:self._fallback(title,text)
        try:self.center.addNotificationRequest_withCompletionHandler_(request,complete)
        except Exception:self._fallback(title,text)

    def notify(self,title,text):
        if self.closed or not self.center:return False
        with self.lock:
            permission=self.permission
            if permission is None:
                self.pending.append((title,text))
                if self.requesting:return True
                self.requesting=True
        if permission is True:
            self._send(title,text);return True
        if permission is False:return False
        def authorized(granted,error):
            with self.lock:
                self.permission=bool(granted and error is None)
                pending=self.pending;self.pending=[];self.requesting=False
            for heading,body in pending:
                if self.permission:self._send(heading,body)
                else:self._fallback(heading,body)
        un=self.notifications
        try:self.center.requestAuthorizationWithOptions_completionHandler_(un.UNAuthorizationOptionAlert|un.UNAuthorizationOptionSound,authorized)
        except Exception:
            with self.lock:
                self.permission=False;self.pending=[];self.requesting=False
            return False
        return True

    def close(self):
        self.closed=True;self.hide()
        with self.lock:self.pending=[]
        self.target.action_callback=lambda action:None
