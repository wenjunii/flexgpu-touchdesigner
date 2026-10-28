<#
.SYNOPSIS
Validates the project-scoped FlexGPU TD Knowledge MCP and Envoy wiring.

.DESCRIPTION
Checks the public FlexGPU project contract and, unless -SkipLocalConfig is
used, the ignored .codex/config.toml. The local check verifies that the
configured Python can load the MCP server CLI and that the knowledge index,
project context, and dynamic Envoy registry exist. The CLI probe only runs
--help with a bounded timeout; it never installs packages or opens a server.
When -RequireEnvoy is supplied, the active registry
entry must reference a live TouchDesigner process with a reachable loopback
port.

Use -WaitReadyMs after a cold TouchDesigner launch to tolerate the bounded
interval while Embody registers the new active instance and starts its
loopback listener. The default remains an immediate single check.

This script never prints machine-local paths and does not start or modify
TouchDesigner. It validates wiring and registration; visual and content
acceptance still require the ordered live audit in docs/EMBODY_MCP.md.
#>
[CmdletBinding()]
param(
    [string]$Config,
    [string]$ProjectContext,
    [string]$EnvoyConfig,
    [switch]$SkipLocalConfig,
    [switch]$RequireEnvoy,
    [ValidateRange(100, 10000)]
    [int]$ConnectTimeoutMs = 2000,
    [ValidateRange(0, 120000)]
    [int]$WaitReadyMs = 0,
    [ValidateRange(100, 30000)]
    [int]$RuntimeTimeoutMs = 10000
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if ([string]::IsNullOrWhiteSpace($Config)) {
    $Config = Join-Path $root '.codex\config.toml'
}
if ([string]::IsNullOrWhiteSpace($ProjectContext)) {
    $ProjectContext = Join-Path $root 'integrations\embody\flexgpu-project-context.json'
}
if ($SkipLocalConfig -and $RequireEnvoy -and [string]::IsNullOrWhiteSpace($EnvoyConfig)) {
    throw '-RequireEnvoy needs the local config or an explicit -EnvoyConfig path.'
}

function Resolve-ExistingPath {
    param(
        [Parameter(Mandatory)]
        [string]$Path,
        [Parameter(Mandatory)]
        [ValidateSet('Leaf', 'Container')]
        [string]$PathType,
        [Parameter(Mandatory)]
        [string]$Label
    )

    if (-not (Test-Path -LiteralPath $Path -PathType $PathType)) {
        throw "$Label does not exist or has the wrong type."
    }
    return [System.IO.Path]::GetFullPath(
        (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path
    )
}

function ConvertFrom-TomlQuotedString {
    param(
        [Parameter(Mandatory)]
        [string]$Token
    )

    if ($Token.StartsWith("'")) {
        return $Token.Substring(1, $Token.Length - 2)
    }
    return ($Token | ConvertFrom-Json)
}

function Get-TomlCommandAndArguments {
    param(
        [Parameter(Mandatory)]
        [string]$Text
    )

    $quoted = "(?:'[^']*'|`"(?:\\.|[^`"\\])*`")"
    $commandMatch = [regex]::Match(
        $Text,
        "(?m)^\s*command\s*=\s*(?<value>$quoted)\s*$"
    )
    if (-not $commandMatch.Success) {
        throw 'The local MCP config has no supported command value.'
    }

    $argsMatch = [regex]::Match(
        $Text,
        '(?ms)^\s*args\s*=\s*\[(?<body>.*?)^\s*\]\s*$'
    )
    if (-not $argsMatch.Success) {
        throw 'The local MCP config has no supported args array.'
    }

    $arguments = @(
        [regex]::Matches($argsMatch.Groups['body'].Value, $quoted) |
            ForEach-Object {
                ConvertFrom-TomlQuotedString -Token $_.Value
            }
    )
    return [pscustomobject]@{
        Command = ConvertFrom-TomlQuotedString -Token $commandMatch.Groups['value'].Value
        Arguments = $arguments
    }
}

function Get-RequiredArgumentValue {
    param(
        [Parameter(Mandatory)]
        [string[]]$Arguments,
        [Parameter(Mandatory)]
        [string]$Name
    )

    $indexes = @(
        for ($index = 0; $index -lt $Arguments.Count; $index++) {
            if ($Arguments[$index] -eq $Name) {
                $index
            }
        }
    )
    if ($indexes.Count -ne 1) {
        throw "The local MCP config must contain exactly one $Name argument."
    }
    $valueIndex = $indexes[0] + 1
    if ($valueIndex -ge $Arguments.Count -or $Arguments[$valueIndex].StartsWith('-')) {
        throw "The local MCP config has no value after $Name."
    }
    return $Arguments[$valueIndex]
}

function Test-TDKnowledgePythonRuntime {
    param(
        [Parameter(Mandatory)][string]$Python,
        [Parameter(Mandatory)][string]$Server,
        [Parameter(Mandatory)][int]$TimeoutMs
    )

    $probe = [System.Diagnostics.Process]::new()
    $probe.StartInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $probe.StartInfo.FileName = $Python
    $probe.StartInfo.Arguments = '-B "' + $Server + '" --help'
    $probe.StartInfo.WorkingDirectory = [IO.Path]::GetDirectoryName($Server)
    $probe.StartInfo.UseShellExecute = $false
    $probe.StartInfo.CreateNoWindow = $true
    $probe.StartInfo.RedirectStandardOutput = $true
    $probe.StartInfo.RedirectStandardError = $true
    try {
        if (-not $probe.Start()) {
            throw 'Could not start the configured TD Knowledge Python CLI probe.'
        }
        # Drain both streams concurrently, but do not expose local paths or
        # dependency diagnostics that could contain private environment data.
        $stdout = $probe.StandardOutput.ReadToEndAsync()
        $stderr = $probe.StandardError.ReadToEndAsync()
        if (-not $probe.WaitForExit($TimeoutMs)) {
            # This handle belongs only to our --help probe, never to the live
            # TouchDesigner process or an existing MCP server.
            $probe.Kill()
            $probe.WaitForExit(1000) | Out-Null
            throw 'The TD Knowledge Python CLI probe timed out; the live project was not stopped.'
        }
        if (-not $stdout.Wait(1000) -or -not $stderr.Wait(1000)) {
            throw 'The TD Knowledge Python CLI probe did not close its output streams.'
        }
        $stdout.GetAwaiter().GetResult() | Out-Null
        $stderr.GetAwaiter().GetResult() | Out-Null
        if ($probe.ExitCode -ne 0) {
            throw (
                'The configured TD Knowledge Python cannot load the server CLI. ' +
                'Use the td-knowledge-mcp project''s own .venv Python, not the ' +
                'td-ai-assistant library environment. No packages were installed.'
            )
        }
    }
    finally {
        $probe.Dispose()
    }
}

function Test-LoopbackPort {
    param(
        [Parameter(Mandatory)]
        [int]$Port,
        [Parameter(Mandatory)]
        [int]$TimeoutMs
    )

    $client = [System.Net.Sockets.TcpClient]::new()
    $asyncResult = $null
    try {
        $asyncResult = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        if (-not $asyncResult.AsyncWaitHandle.WaitOne($TimeoutMs)) {
            return $false
        }
        $client.EndConnect($asyncResult)
        return $client.Connected
    }
    catch {
        return $false
    }
    finally {
        if ($null -ne $asyncResult) {
            $asyncResult.AsyncWaitHandle.Dispose()
        }
        $client.Dispose()
    }
}

function Get-EnvoyRuntimeState {
    param(
        [Parameter(Mandatory)]
        [string]$RegistryPath,
        [Parameter(Mandatory)]
        [int]$TimeoutMs
    )

    $registry = Get-Content -LiteralPath $RegistryPath -Raw | ConvertFrom-Json
    $activeInstance = [string]$registry.active
    $instanceProperty = @(
        $registry.instances.PSObject.Properties |
            Where-Object { $_.Name -eq $activeInstance }
    )
    if ([string]::IsNullOrWhiteSpace($activeInstance) -or $instanceProperty.Count -ne 1) {
        return [pscustomobject]@{
            State = 'offline'
            ActiveInstance = $activeInstance
            ActivePort = $null
            ProcessAlive = $false
            ListenerReachable = $false
        }
    }

    $instance = $instanceProperty[0].Value
    $activePort = [int]$instance.port
    $touchDesignerPid = [int]$instance.td_pid
    if ($activePort -lt 1 -or $activePort -gt 65535 -or $touchDesignerPid -lt 1) {
        throw 'The active FlexGPU Envoy registry entry is malformed.'
    }
    $processAlive = $null -ne (
        Get-Process -Id $touchDesignerPid -ErrorAction SilentlyContinue
    )
    $listenerReachable = Test-LoopbackPort `
        -Port $activePort `
        -TimeoutMs $TimeoutMs
    return [pscustomobject]@{
        State = if ($processAlive -and $listenerReachable) { 'ready' } else { 'offline' }
        ActiveInstance = $activeInstance
        ActivePort = $activePort
        ProcessAlive = $processAlive
        ListenerReachable = $listenerReachable
    }
}

$projectContextPath = Resolve-ExistingPath `
    -Path $ProjectContext `
    -PathType Leaf `
    -Label 'FlexGPU project context'
$context = Get-Content -LiteralPath $projectContextPath -Raw | ConvertFrom-Json

if ($context.schema_version -ne 1) {
    throw 'The FlexGPU project context schema version is unsupported.'
}
if ($context.project_id -ne 'flexgpu-touchdesigner') {
    throw 'The project context does not identify FlexGPU.'
}
$rootOperator = [string]$context.network.root_operator
$identities = @($context.network.identity_operators)
if ($rootOperator -ne '/project1/flexgpu' -or $identities -notcontains $rootOperator) {
    throw 'The project context is missing the FlexGPU live identity guard.'
}
if (-not ([string]$context.network.managed_scope).StartsWith("$rootOperator/")) {
    throw 'The managed scope is outside the guarded FlexGPU root.'
}

$localConfigState = 'skipped'
$pythonRuntimeState = 'skipped'
$envoyRegistryPath = $null
if (-not $SkipLocalConfig) {
    $configPath = Resolve-ExistingPath `
        -Path $Config `
        -PathType Leaf `
        -Label 'Project-scoped Codex MCP config'
    $parsed = Get-TomlCommandAndArguments -Text (
        Get-Content -LiteralPath $configPath -Raw
    )

    Resolve-ExistingPath `
        -Path $parsed.Command `
        -PathType Leaf `
        -Label 'Configured TD Knowledge Python' | Out-Null

    $serverCandidates = @(
        $parsed.Arguments |
            Where-Object { $_ -match '\.py$' }
    )
    if ($serverCandidates.Count -ne 1) {
        throw 'The local MCP config must identify exactly one Python server script.'
    }
    Resolve-ExistingPath `
        -Path $serverCandidates[0] `
        -PathType Leaf `
        -Label 'Configured TD Knowledge server' | Out-Null

    Test-TDKnowledgePythonRuntime `
        -Python $parsed.Command `
        -Server $serverCandidates[0] `
        -TimeoutMs $RuntimeTimeoutMs
    $pythonRuntimeState = 'ready'

    $configuredProjectContext = Resolve-ExistingPath `
        -Path (Get-RequiredArgumentValue `
            -Arguments $parsed.Arguments `
            -Name '--project-context') `
        -PathType Leaf `
        -Label 'Configured FlexGPU project context'
    if ($configuredProjectContext -ne $projectContextPath) {
        throw 'The local MCP config points to a different project context.'
    }

    Resolve-ExistingPath `
        -Path (Get-RequiredArgumentValue `
            -Arguments $parsed.Arguments `
            -Name '--faiss-db') `
        -PathType Container `
        -Label 'Configured TD knowledge index' | Out-Null

    $envoyRegistryPath = Resolve-ExistingPath `
        -Path (Get-RequiredArgumentValue `
            -Arguments $parsed.Arguments `
            -Name '--envoy-config') `
        -PathType Leaf `
        -Label 'Configured FlexGPU Envoy registry'

    $fallbackPort = [int](Get-RequiredArgumentValue `
        -Arguments $parsed.Arguments `
        -Name '--port')
    if ($fallbackPort -ne 9870) {
        throw 'The FlexGPU Envoy fallback port must remain 9870.'
    }
    $localConfigState = 'valid'
}
elseif (-not [string]::IsNullOrWhiteSpace($EnvoyConfig)) {
    $envoyRegistryPath = Resolve-ExistingPath `
        -Path $EnvoyConfig `
        -PathType Leaf `
        -Label 'FlexGPU Envoy registry'
}

$envoyState = 'not_checked'
$activeInstance = $null
$activePort = $null
$processAlive = $false
$listenerReachable = $false
if ($null -ne $envoyRegistryPath) {
    $deadline = [DateTime]::UtcNow.AddMilliseconds($WaitReadyMs)
    do {
        $snapshot = Get-EnvoyRuntimeState `
            -RegistryPath $envoyRegistryPath `
            -TimeoutMs $ConnectTimeoutMs
        $envoyState = $snapshot.State
        $activeInstance = $snapshot.ActiveInstance
        $activePort = $snapshot.ActivePort
        $processAlive = $snapshot.ProcessAlive
        $listenerReachable = $snapshot.ListenerReachable

        if (-not $RequireEnvoy -or $envoyState -eq 'ready' -or $WaitReadyMs -eq 0) {
            break
        }
        $remainingMs = [int][Math]::Ceiling(($deadline - [DateTime]::UtcNow).TotalMilliseconds)
        if ($remainingMs -le 0) {
            break
        }
        Start-Sleep -Milliseconds ([Math]::Min(250, $remainingMs))
    } while ([DateTime]::UtcNow -lt $deadline)

    if ($RequireEnvoy -and $envoyState -ne 'ready') {
        throw (
            "The active FlexGPU Envoy process or loopback listener is unavailable " +
            "after waiting $WaitReadyMs ms. Preview stale registrations with " +
            "scripts\Repair-FlexShowEnvoyRegistry.ps1, then use -Repair only " +
            "after reviewing its output. Also check that Embody Envoy Enable " +
            "is on and the project timeline can advance during startup."
        )
    }
}
elseif ($RequireEnvoy) {
    throw 'No FlexGPU Envoy registry was supplied.'
}

[ordered]@{
    status = 'ok'
    project_id = [string]$context.project_id
    root_operator = $rootOperator
    managed_scope = [string]$context.network.managed_scope
    local_config = $localConfigState
    python_runtime = $pythonRuntimeState
    envoy = $envoyState
    active_instance = $activeInstance
    active_port = $activePort
    process_alive = $processAlive
    listener_reachable = $listenerReachable
} | ConvertTo-Json -Compress
