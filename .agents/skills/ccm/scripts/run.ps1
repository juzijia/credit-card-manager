param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ScriptArgs
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ToolScript = Join-Path $ScriptDir "ccm_api.py"

# Locate Python
$pythonCmd = "python"
if (Test-Path "C:UsersorangeAppDataLocalProgramsPythonPython313python.exe") {
    $pythonCmd = "C:UsersorangeAppDataLocalProgramsPythonPython313python.exe"
}

& $pythonCmd $ToolScript @ScriptArgs
exit $LASTEXITCODE
