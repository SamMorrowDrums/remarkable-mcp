[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
$helper = Join-Path $PSScriptRoot 'register_remarkable_cloud.py'
$tokenFile = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.rmapi'
if (Test-Path -LiteralPath $tokenFile) { throw 'A cloud credential already exists. It was not replaced.' }
if (-not (Test-Path -LiteralPath $python)) { throw 'The installed MCP Python environment is missing.' }

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$form = New-Object System.Windows.Forms.Form
$form.Text = 'reMarkable Cloud - private registration'
$form.ClientSize = New-Object System.Drawing.Size(590, 210)
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.MinimizeBox = $false
$form.TopMost = $true
$label = New-Object System.Windows.Forms.Label
$label.Text = "Sign in at my.remarkable.com/device/desktop/connect.`r`nPaste the new eight-character desktop code below.`r`nOnly the credential is saved to your private user profile; no secret is logged."
$label.Location = New-Object System.Drawing.Point(16, 16)
$label.Size = New-Object System.Drawing.Size(558, 70)
$box = New-Object System.Windows.Forms.TextBox
$box.Location = New-Object System.Drawing.Point(16, 98)
$box.Size = New-Object System.Drawing.Size(558, 24)
$box.UseSystemPasswordChar = $true
$ok = New-Object System.Windows.Forms.Button
$ok.Text = 'Register'
$ok.Location = New-Object System.Drawing.Point(350, 156)
$ok.Size = New-Object System.Drawing.Size(108, 28)
$ok.Add_Click({
    if ($box.Text.Trim() -notmatch '^[A-Za-z0-9]{8}$') {
        [System.Windows.Forms.MessageBox]::Show('Enter the eight-character desktop code, without spaces.', 'Code format') | Out-Null
        return
    }
    $form.DialogResult = [System.Windows.Forms.DialogResult]::OK
    $form.Close()
})
$cancel = New-Object System.Windows.Forms.Button
$cancel.Text = 'Cancel'
$cancel.Location = New-Object System.Drawing.Point(470, 156)
$cancel.Size = New-Object System.Drawing.Size(104, 28)
$cancel.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
$form.Controls.AddRange(@($label, $box, $ok, $cancel))
$form.AcceptButton = $ok
$form.CancelButton = $cancel
$code = $null
$createdToken = $false
try {
    if ($form.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
        Write-Output 'Registration cancelled; no credential was saved.'
        return
    }
    $code = $box.Text.Trim()
    $box.Clear()
    # CreateNew protects any credential created by another process while the dialog was open.
    $file = [IO.File]::Open($tokenFile, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    $file.Dispose()
    $createdToken = $true
    $acl = New-Object System.Security.AccessControl.FileSecurity
    $acl.SetAccessRuleProtection($true, $false)
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().User
    foreach ($sid in @($identity, [Security.Principal.SecurityIdentifier]::new('S-1-5-18'), [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))) {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new($sid, 'FullControl', 'Allow')
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $tokenFile -AclObject $acl

    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $python
    $info.Arguments = '"' + $helper + '"'
    $info.WorkingDirectory = $PSScriptRoot
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardInput = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $info
    if (-not $process.Start()) { throw 'Registration helper could not start.' }
    $process.StandardInput.WriteLine($code)
    $process.StandardInput.Close()
    $code = $null
    $safeOutput = $process.StandardOutput.ReadToEnd()
    $discardedError = $process.StandardError.ReadToEnd()
    $process.WaitForExit()
    $discardedError = $null
    if ($process.ExitCode -ne 0) { throw 'Registration did not complete. Generate a new desktop code before trying again.' }
    Write-Output $safeOutput.Trim()
} finally {
    $code = $null
    $form.Dispose()
    if ($createdToken -and (Test-Path -LiteralPath $tokenFile) -and (Get-Item -LiteralPath $tokenFile).Length -eq 0) {
        Remove-Item -LiteralPath $tokenFile
    }
}
