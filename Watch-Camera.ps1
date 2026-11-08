$ErrorActionPreference = 'Stop'
& python (Join-Path $PSScriptRoot 'camera_client.py') --watch
exit $LASTEXITCODE
