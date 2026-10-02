param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
$buildRoot = Join-Path $PSScriptRoot '.build-env'
& $Python -m venv $buildRoot
if ($LASTEXITCODE -ne 0) { throw '创建构建环境失败' }
$buildPython = Join-Path $buildRoot 'Scripts/python.exe'
& $buildPython -m pip install 'pyinstaller==6.22.3'
if ($LASTEXITCODE -ne 0) { throw '安装打包工具失败' }
$icon = Join-Path $PSScriptRoot 'app.ico'
$entry = Join-Path $PSScriptRoot 'windows_app.py'
$target = Split-Path $PSScriptRoot -Parent
& $buildPython -m PyInstaller --noconfirm --onefile --windowed --name CodexUsageCollector --icon $icon --add-data "${icon}:." --distpath $target --workpath (Join-Path $PSScriptRoot '.build') --specpath (Join-Path $PSScriptRoot '.build') $entry
if ($LASTEXITCODE -ne 0) { throw '打包失败' }
