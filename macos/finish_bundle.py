"""Finalize bundle metadata before signing; never patch an already-distributed app."""
from pathlib import Path
import plistlib
import sys

app=Path(sys.argv[1]).resolve()
p=app/'Contents'/'Info.plist'
with p.open('rb') as stream:data=plistlib.load(stream)
data.update(CFBundleIdentifier='xyz.814305.codex-quota-collector',CFBundleName='CodexUsageCollector',
            CFBundleDisplayName='Codex 用量采集器',CFBundleShortVersionString='1.3.0',CFBundleVersion='130',
            LSMinimumSystemVersion='11.0',NSHighResolutionCapable=True,NSPrincipalClass='NSApplication')
with p.open('wb') as stream:plistlib.dump(data,stream)
