#requires -Version 7.0

[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$running = (& docker inspect --format '{{.State.Running}}' wechat-selkies 2>$null)
if ($LASTEXITCODE -ne 0 -or $running -ne 'true') {
    [Console]::Error.WriteLine('wechat-selkies 容器没有运行。')
    exit 2
}

$dockerArgs = @(
    'exec', '-i', '-u', 'abc',
    '-e', 'PYTHONPATH=/opt/wechat-history/site-packages:/opt/wechat-history',
    '-e', 'PYTHONDONTWRITEBYTECODE=1',
    '-e', 'DISPLAY=:1',
    'wechat-selkies',
    '/lsiopy/bin/python3', '-m', 'wechat_history.mcp_server'
)
& docker @dockerArgs
exit $LASTEXITCODE
