# Prusa Watch dead man's switch: CloudWatch alarm on a MISSING heartbeat -> SNS email.
# Run once from PowerShell with an AWS admin profile:
#   .\aws\heartbeat-alarm.ps1 -Email you@example.com [-AwsProfile default] [-Region eu-central-1]
# Requires aws/iam-policy.json with ACCOUNT_ID and FROM_ADDRESS filled in.
# Idempotent: re-running updates the same topic/alarm.
param(
    [Parameter(Mandatory = $true)][string]$Email,
    [string]$AwsProfile = "default",
    [string]$Region = "eu-central-1"
)

$ErrorActionPreference = "Stop"
$Here    = Split-Path -Parent $MyInvocation.MyCommand.Path

# 1. Let the add-on's IAM user publish the heartbeat metric (namespace PrusaWatch only).
aws iam put-user-policy --user-name prusa-watch --policy-name prusa-watch `
    --policy-document "file://$Here/iam-policy.json" --profile $AwsProfile

# 2. SNS topic + email subscription (AWS sends a confirmation mail - click the link once).
$Topic = aws sns create-topic --name prusa-watch-alerts --region $Region --profile $AwsProfile `
    --tags Key=project,Value=prusa-watch --query TopicArn --output text
$Existing = aws sns list-subscriptions-by-topic --topic-arn $Topic --region $Region --profile $AwsProfile `
    --query "Subscriptions[?Endpoint=='$Email'].SubscriptionArn" --output text
if (-not $Existing) {
    aws sns subscribe --topic-arn $Topic --protocol email --notification-endpoint $Email `
        --region $Region --profile $AwsProfile | Out-Null
    Write-Host "Confirmation email sent to $Email - click 'Confirm subscription' in it."
}

# 3. Alarm: fewer than 1 heartbeat in each of 3 consecutive 5-minute periods (= silent for 15 min).
#    Missing data counts as breaching, which is the whole point of a dead man's switch.
aws cloudwatch put-metric-alarm --region $Region --profile $AwsProfile `
    --alarm-name prusa-watch-heartbeat `
    --alarm-description "Prusa Watch add-on on the HA Pi has stopped reporting (add-on crashed, Pi off, or no internet). Your print is NOT being watched." `
    --namespace PrusaWatch --metric-name Heartbeat --statistic SampleCount `
    --period 300 --evaluation-periods 3 --datapoints-to-alarm 3 `
    --threshold 1 --comparison-operator LessThanThreshold `
    --treat-missing-data breaching `
    --alarm-actions $Topic --ok-actions $Topic `
    --tags Key=project,Value=prusa-watch

Write-Host "Done. Topic: $Topic"
aws cloudwatch describe-alarms --alarm-names prusa-watch-heartbeat --region $Region --profile $AwsProfile `
    --query "MetricAlarms[0].{State:StateValue,Reason:StateReason}" --output table
