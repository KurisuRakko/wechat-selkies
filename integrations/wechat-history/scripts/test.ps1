#requires -Version 7.0

[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$dockerArgs = @(
    'exec', '-u', 'abc',
    '-e', 'PYTHONPATH=/opt/wechat-history/site-packages:/opt/wechat-history',
    '-e', 'PYTHONDONTWRITEBYTECODE=1',
    'wechat-selkies',
    '/lsiopy/bin/python3', '-m', 'unittest', 'discover',
    '-s', '/opt/wechat-history/tests',
    '-t', '/opt/wechat-history', '-v'
)
& docker @dockerArgs
exit $LASTEXITCODE
