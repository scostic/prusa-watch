# Prusa Watch

AI watchdog for a Prusa printer with a Buddy3D camera. While a print runs it grabs a camera frame every
minute, sends it with PrusaLink telemetry to Claude Haiku 4.5 on Amazon Bedrock, and emails you
(Amazon SES) when the print fails. Full guide: https://github.com/scostic/prusa-watch

## Before you start
- PrusaLink enabled on the printer (API key from *Settings → Network → PrusaLink*).
- Camera RTSP enabled: Prusa Connect → printer → *Camera* → *Streaming* = **RTSP**
  (stream at `rtsp://<camera-ip>/live`).
- An AWS IAM user with the policy from `aws/iam-policy.json` (Bedrock Claude Haiku 4.5 + SES send).

## Options

| Option | Meaning |
|---|---|
| `printer_host` | PrusaLink IP or hostname |
| `prusalink_api_key` | PrusaLink API key (or use `prusalink_username` / `prusalink_password` digest login) |
| `camera_url` / `camera_rotate` | RTSP URL; rotate frames 0/90/180/270° if the stream is upside down |
| `aws_region`, `aws_access_key_id`, `aws_secret_access_key` | the IAM user's key |
| `bedrock_model_id` | default `eu.anthropic.claude-haiku-4-5-20251001-v1:0` (EU cross-region profile) |
| `email_from`, `email_to` | SES-verified sender; comma-separated recipients |
| `check_interval_s` | seconds between checks while printing (60) |
| `compare_minutes` | age of the reference frame used to judge growth (5) |
| `failure_threshold`, `min_confidence` | consecutive confident failure verdicts before an email (3, 0.6) |
| `confirm_failures` | re-check every failure on a second frame before it counts (on) |
| `alert_cooldown_min` | minimum minutes between repeated failure emails (30) |
| `notify_finished` | email when a print finishes (on) |
| `auto_action`, `auto_action_threshold` | `none` / `pause` / `stop` after N consecutive failures (off by default) |
| `power_entity`, `energy_price`, `currency` | optional smart plug power sensor (W) → energy and cost per print |
| `heartbeat_min`, `cloudwatch_heartbeat`, `blind_alert_min` | proof-of-life and "watchdog blind" alert |
| `hec_url`, `hec_token`, `hec_index`, `hec_verify_tls` | optional Splunk HEC output |

## Outputs
- `sensor.prusa_watch`: state = latest verdict while printing (`ok` / `warning` / `failure` /
  `camera_problem`), otherwise the printer state (`idle`, `paused`, `attention`, `finished`, `offline`...).
  Attributes: `issue`, `confidence`, `description`, `part_visible`, `streak`, `last_check`, `printer_state`,
  `job_id`, `progress`, `time_remaining`, `axis_z`, `temp_nozzle`, `target_nozzle`, `temp_bed`, `target_bed`,
  and with a smart plug `power_w`, `print_energy_kwh`, `print_energy_cost`.
- `/share/prusa_watch/latest.jpg` and `flagged-*.jpg` for warnings/failures.
- Emails: possible failure, printer attention, camera unreachable, watchdog blind, print finished
  (with energy and cost when `power_entity` is set).
- Optional Splunk events (`type` = `check`, `state`, `email`, `error`, `heartbeat`, `energy`).

## Labelling (test set)
Open **Prusa Watch** in the Home Assistant sidebar to label saved checks as ✅ OK print or ❌ Real failure.
For quick labels, create three *Button* helpers (Settings → Devices & services → Helpers → Create helper →
Button) named **Prusa Watch Correct**, **Prusa Watch False alarm** and **Prusa Watch Missed failure**
(entity IDs `input_button.prusa_watch_correct`, `..._false_alarm`, `..._missed_failure`), and put them on a
dashboard. Options: `dataset_enabled`, `dataset_ok_every`, `dataset_max`, `label_button_*`.

## Smart plug
Set `power_entity` to the plug's power sensor in W (e.g. `sensor.<plug>_current_consumption`) and
`energy_price` to your price per kWh. An example automation that switches the plug off after the print, once
the nozzle is below 50 °C, is in the repository under `ha/`.

Keep `auto_action: none` until you have watched a few prints and tested a deliberate failure.
This is a convenience monitor, not a safety device - never leave a printer unattended if that is unsafe.
