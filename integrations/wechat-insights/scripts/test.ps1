#requires -Version 7.0

[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$dockerArgs = @(
    'exec',
    'wechat-insights',
    'python3', '-m', 'unittest', 'discover',
    '-s', '/opt/wechat-insights/tests',
    '-t', '/opt/wechat-insights', '-v'
)
& docker @dockerArgs
exit $LASTEXITCODE
