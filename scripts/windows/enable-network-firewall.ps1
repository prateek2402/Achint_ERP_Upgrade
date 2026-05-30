# Opens Windows Firewall for AchintERP LAN access.
# Usage (elevated): .\enable-network-firewall.ps1 [-Port 3000] [-RemoteSubnet "192.168.1.0/24"]

param(
    [int]$Port = 3000,
    [string]$RemoteSubnet = ""
)

$ErrorActionPreference = "Stop"

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $isAdmin) {
    Write-Error "Run PowerShell or enable-network-firewall.cmd as Administrator."
}

$ruleName = "AchintERP-TCP-$Port"
$existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
if ($existing) {
    Remove-NetFirewallRule -DisplayName $ruleName
}

$params = @{
    DisplayName  = $ruleName
    Description  = "Inbound TCP for Achint ERP web app (port $Port)"
    Direction    = "Inbound"
    Action       = "Allow"
    Protocol     = "TCP"
    LocalPort    = $Port
    Profile      = "Domain", "Private"
    Enabled      = True
}
if ($RemoteSubnet) {
    $params.RemoteAddress = $RemoteSubnet
    Write-Host "Allowing only from subnet: $RemoteSubnet"
} else {
    Write-Host "Allowing from any remote address on Private/Domain profiles."
    Write-Host "Tip: restrict with -RemoteSubnet '192.168.1.0/24'"
}

New-NetFirewallRule @params | Out-Null
Write-Host "Firewall rule '$ruleName' created for TCP port $Port."

Write-Host ""
Write-Host "LAN URLs (use on other PCs):"
Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -notlike "127.*" -and $_.PrefixOrigin -ne "WellKnown" } |
    ForEach-Object { Write-Host "  http://$($_.IPAddress):$Port/" }
