# Shared interpreter guard for the isolated model runtimes. Preview paths do
# not invoke it; installation must fail before creating/upgrading a 3.10 venv.
function Assert-FlexGpuWorkerPython {
    param([Parameter(Mandatory = $true)][string]$Python)

    # A pipeline also waits for GUI-subsystem Python launchers on Windows.
    & $Python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'The patched worker dependencies require Python 3.11 or newer. Create a separate environment; do not upgrade the only working show environment in place.'
    }
}
