param(
  [Parameter(Mandatory = $true, Position = 0)]
  [ValidateSet('up', 'bootstrap', 'status', 'snapshot', 'down', 'destroy')]
  [string]$Task
)

# Isolated, persistent stack that collects public data for the real-data chat eval (docs/development.md).
# It never touches the dev stack (umwelt-os-*) or the disposable test stacks: own project name, ports and keys.
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$project = 'umwelt-evalcollect'
$envFile = Join-Path $repo '.env.eval-collect'
# Credential and secret-bearing tables are left out of the dump (schema only): the stack's keys never leave it and
# test defaults are public. Every table with an encrypted_* / token_hash / secret column belongs here; the dump is
# verified against this list after pg_dump, so a missing entry fails the snapshot instead of leaking data.
$secretTables = @(
  'connector_managed_credentials', 'connector_native_credentials', 'connector_rest_credentials',
  'connector_world_credentials', 'collector_credentials', 'automation_webhook_credentials', 'auth_session',
  'connector_oauth_attempts', 'connector_oauth_grants', 'dashboard_live_credentials',
  'github_oauth_attempts', 'github_oauth_grants', 'mcp_connections'
)
# ai_settings is needed by the app, so its rows are dumped; the snapshot refuses to run while any ciphertext
# value is stored there (the eval stack never saves an AI or search key; a non-empty value means someone did).
$aiSecretGuardSql = @"
SELECT count(*) FROM ai_settings
WHERE web_search_api_key_ciphertext IS NOT NULL
   OR (providers::text ~ 'ciphertext"\s*:\s*"[^"]')
   OR (n8n::text ~ 'ciphertext"\s*:\s*"[^"]')
   OR (web_search_fallbacks::text ~ 'ciphertext"\s*:\s*"[^"]');
"@
Push-Location $repo
try {
  function Invoke-Checked([string]$Program, [string[]]$Arguments) {
    <#
    .SYNOPSIS
    Run an external program and fail the task when it exits unsuccessfully.

    .PARAMETER Program
    Executable name or path.

    .PARAMETER Arguments
    Argument vector passed without joining it into a command string.

    .NOTES
    Throws when the native exit code is nonzero.
    #>
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program failed with exit code $LASTEXITCODE" }
  }

  function Get-ComposeArgs {
    <#
    .SYNOPSIS
    Return the fixed `docker compose` argument prefix for the collection project (no profiles).

    .OUTPUTS
    System.String[]
    #>
    if ($project -ne 'umwelt-evalcollect') { throw "Refusing to run against project '$project'" }
    return @('compose', '-p', $project, '-f', 'docker-compose.yml', '-f', 'docker-compose.test.yml')
  }

  function Set-StackEnvironment {
    <#
    .SYNOPSIS
    Export the ports and the two credential-encryption keys the collection stack needs to this process.

    .NOTES
    Creates the git-ignored .env.eval-collect with two random Fernet-compatible keys on first use, reusing
    scripts/ensure_env.py. Key values are never printed. Changing the file later makes stored credentials unreadable.
    #>
    $env:WEB_PORT = '43300'
    $env:API_TEST_PORT = '48000'
    $env:TEST_POSTGRES_PORT = '45432'
    $env:TEST_PUBLIC_ORIGIN = 'http://localhost:43300'
    if (-not (Test-Path $envFile)) {
      $connector = (& uv run python -c "from scripts.ensure_env import _fernet_key; print(_fernet_key())").Trim()
      if ($LASTEXITCODE -ne 0) { throw 'Could not generate the connector key' }
      $ai = (& uv run python -c "from scripts.ensure_env import _fernet_key; print(_fernet_key())").Trim()
      if ($LASTEXITCODE -ne 0) { throw 'Could not generate the AI key' }
      Set-Content -Path $envFile -Encoding ascii -Value @(
        "TEST_CONNECTOR_CREDENTIAL_ENCRYPTION_KEY=$connector",
        "TEST_AI_CREDENTIAL_ENCRYPTION_KEY=$ai"
      )
    }
    foreach ($line in Get-Content $envFile) {
      if ($line -match '^(TEST_(?:CONNECTOR|AI)_CREDENTIAL_ENCRYPTION_KEY)=(.+)$') {
        [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
      }
    }
    foreach ($name in 'TEST_CONNECTOR_CREDENTIAL_ENCRYPTION_KEY', 'TEST_AI_CREDENTIAL_ENCRYPTION_KEY') {
      if (-not [Environment]::GetEnvironmentVariable($name, 'Process')) { throw "$name missing from .env.eval-collect" }
    }
  }

  function Invoke-Psql([string]$Sql) {
    <#
    .SYNOPSIS
    Run one read-only query in the collection database and return its unaligned rows.

    .PARAMETER Sql
    SQL text passed as a single argument to `psql -At`.
    #>
    $compose = Get-ComposeArgs
    Invoke-Checked 'docker' ($compose + @('exec', '-T', 'postgres', 'psql', '-U', 'bbd_test', '-d', 'bbd_test', '-At', '-F', ' | ', '-c', $Sql))
  }

  function Show-Status {
    <#
    .SYNOPSIS
    Print service state, per-source collection health and the database size.

    .NOTES
    Documents have no collected_at column, so their freshness is created_at; observations use collected_at.
    #>
    $compose = Get-ComposeArgs
    Invoke-Checked 'docker' ($compose + @('ps'))
    Invoke-Psql @"
SELECT coalesce(s.provider, s.name) AS source, s.status, coalesce(s.last_error_code, '-') AS last_error,
  (SELECT count(*) FROM documents d WHERE d.source_id = s.id) AS docs,
  (SELECT count(*) FROM source_observations o WHERE o.source_id = s.id) AS obs,
  greatest((SELECT max(d.created_at) FROM documents d WHERE d.source_id = s.id),
           (SELECT max(o.collected_at) FROM source_observations o WHERE o.source_id = s.id)) AS last_collected
FROM sources s ORDER BY 1;
"@
    # Separate call: psql -c prints only the last result of a multi-statement string.
    Invoke-Psql "SELECT 'db_size', pg_size_pretty(pg_database_size('bbd_test'))"
  }

  function Assert-NoSecretTableData {
    <#
    .SYNOPSIS
    Fail when the dump in the container carries table data for any secret-bearing table.

    .NOTES
    Reads the archive's table of contents with `pg_restore -l` (no data is restored). A TABLE DATA entry matching a
    name in $secretTables means --exclude-table-data was not applied, so the dump must not leave the container.
    #>
    $compose = Get-ComposeArgs
    $toc = (Invoke-Checked 'docker' ($compose + @('exec', '-T', 'postgres', 'pg_restore', '-l', '/tmp/snap.dump'))) | Out-String
    $leaked = @($secretTables | Where-Object { $toc -match "TABLE DATA\s+public\s+$([regex]::Escape($_))\s" })
    if ($leaked.Count -gt 0) { throw "Snapshot refused: dump contains table data for: $($leaked -join ', ')" }
  }

  function New-Snapshot {
    <#
    .SYNOPSIS
    Freeze the collected data into data/chat-eval-real/<UTC timestamp>/snapshot.dump plus meta.json.

    .NOTES
    Stops the collecting workers first so the dump is a stable cut (resume with the `up` task). pg_dump writes inside
    the container and the file is copied out with `docker compose cp`; never pipe pg_dump through PowerShell redirection
    because it re-encodes binary. Credential tables are dumped schema-only (see $secretTables).
    Aborts before any dump when ai_settings holds a ciphertext value, and fails if the dump's TOC contains table data
    for a secret table (checked with pg_restore -l inside the container).
    #>
    $compose = Get-ComposeArgs
    $leaked = (Invoke-Psql $aiSecretGuardSql | Out-String).Trim()
    if ($leaked -ne '0') { throw "Snapshot refused: ai_settings holds $leaked ciphertext value(s); clear them before dumping." }
    Invoke-Checked 'docker' ($compose + @('stop', 'cron-worker', 'worker', 'chat-worker', 'model-worker', 'search-build-worker'))
    $stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
    $dir = Join-Path $repo "data/chat-eval-real/$stamp"
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $excludes = $secretTables | ForEach-Object { "--exclude-table-data=$_" }
    Invoke-Checked 'docker' ($compose + @('exec', '-T', 'postgres', 'pg_dump', '-U', 'bbd_test', '-d', 'bbd_test', '-Fc', '-f', '/tmp/snap.dump') + $excludes)
    Assert-NoSecretTableData
    Invoke-Checked 'docker' ($compose + @('cp', 'postgres:/tmp/snap.dump', (Join-Path $dir 'snapshot.dump')))
    Invoke-Checked 'uv' @('run', 'python', '-m', 'tests.integration.chat_eval.snapshot_meta', $dir)
    Write-Host "Snapshot written to $dir"
  }

  Set-StackEnvironment
  switch ($Task) {
    'up' { Invoke-Checked 'docker' ((Get-ComposeArgs) + @('up', '-d', '--build')) }
    'bootstrap' {
      $env:PLAYWRIGHT_BASE_URL = 'http://localhost:43300'
      $env:API_URL = 'http://localhost:48000'
      $env:PLAYWRIGHT_EXTERNAL_SERVER = '1'
      $env:E2E_SETUP_TOKEN = 'bbd-os-disposable-test-token'
      $env:PLAYWRIGHT_RUN_ID = 'evalcollect'
      $env:EVAL_COLLECT = '1'
      # auth.spec.ts (project "bootstrap") creates the owner and the storage state the second spec reuses; it runs once per stack.
      Invoke-Checked 'npx' @('playwright', 'test', 'tests/e2e/auth.spec.ts', 'tests/e2e/eval-collect-bootstrap.spec.ts')
    }
    'status' { Show-Status }
    'snapshot' { New-Snapshot }
    'down' { Invoke-Checked 'docker' ((Get-ComposeArgs) + @('down')) }
    'destroy' {
      $answer = Read-Host "This deletes ALL collected data in project $project. Type '$project' to confirm"
      if ($answer -ne $project) { throw 'Destroy cancelled' }
      Invoke-Checked 'docker' ((Get-ComposeArgs) + @('down', '--volumes'))
    }
  }
}
finally { Pop-Location }
