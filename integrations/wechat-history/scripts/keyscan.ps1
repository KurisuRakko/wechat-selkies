#requires -Version 7.0

[CmdletBinding()]
param(
    [ValidateRange(0, [int]::MaxValue)]
    [int] $WeChatPid = 0
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$scanExitCode = 0
Push-Location $repoRoot
try {
    $running = (& docker inspect --format '{{.State.Running}}' wechat-selkies 2>$null)
    if ($LASTEXITCODE -ne 0 -or $running -ne 'true') {
        throw 'wechat-selkies 容器没有运行。'
    }

    $dockerArgs = @(
        'compose', '--profile', 'history-keyscan',
        'run', '--rm', '--no-deps', '-T', 'history-keyscan'
    )
    if ($WeChatPid -gt 0) {
        $dockerArgs += @('--pid', $WeChatPid.ToString())
    }
    & docker @dockerArgs
    $scanExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

if ($scanExitCode -ne 0) {
    [Console]::Error.WriteLine("密钥扫描失败，docker 退出码为 $scanExitCode。")
    exit $scanExitCode
}
