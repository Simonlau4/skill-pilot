$ErrorActionPreference = 'Stop'
$sourceRoot = Split-Path $PSScriptRoot -Parent
$python = (Get-Command python.exe -ErrorAction Stop).Source
& $python -X utf8 -B (Join-Path $sourceRoot 'scripts/install.py')
if ($LASTEXITCODE) { throw 'Windows 安装没有完成，请查看上面的原因。' }
