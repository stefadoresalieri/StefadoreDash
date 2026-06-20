$python = "C:\Users\blank\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$app = Join-Path $PSScriptRoot "server.py"
& $python $app
