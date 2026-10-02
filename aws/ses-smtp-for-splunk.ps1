# Optional: creates Amazon SES SMTP credentials so a Splunk server can send alert email through SES.
# Run it yourself - it prints the SMTP password once; nothing is written to disk.
#   powershell -ExecutionPolicy Bypass -File .\aws\ses-smtp-for-splunk.ps1 -FromAddr alerts@example.com
param(
    [Parameter(Mandatory = $true)][string]$FromAddr,   # must be a verified SES identity
    [string]$AwsProfile = "default",
    [string]$Region = "eu-central-1",
    [string]$User = "splunk-ses-smtp"
)

$ErrorActionPreference = "Stop"

$policy = @"
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "SplunkAlertMailFromOneAddress",
    "Effect": "Allow",
    "Action": "ses:SendRawEmail",
    "Resource": "*",
    "Condition": { "StringEquals": { "ses:FromAddress": "$FromAddr" } }
  }]
}
"@
$policyFile = New-TemporaryFile
Set-Content $policyFile $policy -Encoding ascii

$exists = $true
try { aws iam get-user --user-name $User --profile $AwsProfile *> $null; if ($LASTEXITCODE) { $exists = $false } } catch { $exists = $false }
if (-not $exists) {
    aws iam create-user --user-name $User --tags Key=purpose,Value=splunk-alert-email --profile $AwsProfile | Out-Null
}
aws iam put-user-policy --user-name $User --policy-name ses-smtp-send --policy-document "file://$policyFile" --profile $AwsProfile
Remove-Item $policyFile

$key = aws iam create-access-key --user-name $User --profile $AwsProfile | ConvertFrom-Json
$id, $secret = $key.AccessKey.AccessKeyId, $key.AccessKey.SecretAccessKey

# SES SMTP password = versioned HMAC-SHA256 chain over the secret key (AWS-documented algorithm).
function HmacSha256([byte[]]$k, [string]$msg) {
    $h = New-Object System.Security.Cryptography.HMACSHA256 (, $k)
    return $h.ComputeHash([Text.Encoding]::UTF8.GetBytes($msg))
}
$sig = [Text.Encoding]::UTF8.GetBytes("AWS4" + $secret)
foreach ($part in @("11111111", $Region, "ses", "aws4_request", "SendRawEmail")) { $sig = HmacSha256 $sig $part }
$smtpPassword = [Convert]::ToBase64String([byte[]](@(0x04) + $sig))

Write-Host ""
Write-Host "=== Enter these in Splunk: Settings > Server settings > Email settings ===" -ForegroundColor Green
Write-Host "Mail host      : email-smtp.$Region.amazonaws.com:587"
Write-Host "Email security : Enable TLS"
Write-Host "Username       : $id"
Write-Host "Password       : $smtpPassword"
Write-Host "Send emails as : $FromAddr"
Write-Host ""
Write-Host "The password is shown only now. To rotate: delete the access key of '$User' in IAM and re-run." -ForegroundColor Yellow
