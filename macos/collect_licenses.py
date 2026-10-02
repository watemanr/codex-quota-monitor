"""Collect dependency notices from the build environment for app redistribution."""
from importlib import metadata
from pathlib import Path
import shutil
import sys


DEPENDENCIES=('pyobjc-core','pyobjc-framework-Cocoa','pyobjc-framework-Security',
              'pyobjc-framework-UserNotifications','pyinstaller')
TEXT_LICENSE_SUFFIXES={'','.txt','.md','.rst'}


def is_license_file(path):
    """Accept conventional text notices, never similarly named binaries/debug files."""
    name=Path(path).name.lower()
    stem=name.split('.',1)[0]
    return (stem in ('license','copying') or stem.startswith(('license-','copying-'))) \
        and Path(name).suffix in TEXT_LICENSE_SUFFIXES


def collect_notices(target):
    target=Path(target)
    # A reused build directory must not retain files accepted by an older collector.
    if target.exists():shutil.rmtree(target)
    target.mkdir(parents=True)
    for source in (Path(__file__).resolve().parent.parent/'windows'/'licenses').glob('*'):
        if source.is_file():shutil.copy2(source,target/source.name)
    for name in DEPENDENCIES:
        dist=metadata.distribution(name);found=False
        for file in dist.files or []:
            if not is_license_file(file):continue
            source=Path(dist.locate_file(file))
            if source.is_file():
                shutil.copy2(source,target/(name+'-'+source.name));found=True
        if not found:
            # METADATA retains the package's license declaration and attribution.
            text=dist.read_text('METADATA')
            if text:(target/(name+'-METADATA.txt')).write_text(text,encoding='utf8')


def main():
    if len(sys.argv)!=2:raise SystemExit('usage: collect_licenses.py TARGET_DIR')
    collect_notices(sys.argv[1])


if __name__=='__main__':main()
