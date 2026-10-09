# Changelog

## 0.8.3
- No AI checks while the printer heats up: PrusaLink reports PRINTING during preheat and bed levelling, so
  checks now start once nozzle and bed are within 5 °C of target (or progress > 0). Saves calls and avoids
  confused warnings; the BLIND timer starts after preheat. HA shows `printing` with `phase: preheat`.
  Option `skip_preheat` (on).
- Confirmed on a real print: Buddy firmware serves the G-code thumbnail as PNG at `/thumb/l/<storage>/<file>`.

## 0.8.2
- **AI provider self-check at startup**: a free Models API lookup (Claude API) or a 1-token request (Bedrock)
  logs `AI provider check OK` or the exact problem (401 key, 403 access, 404 model), sends a `type=ai_check`
  event, and emails you on configuration errors - so a bad key is found before a print, not during one.

## 0.8.1
- Heartbeat events carry `provider` and `model`, so Splunk shows which model is active even between prints.
- AI errors are logged as `AI call failed (<provider>:<model>)` with `component=ai` (previously always "Bedrock").

## 0.8.0
- **Claude API as an alternative to Bedrock**: `ai_provider: anthropic` + `anthropic_api_key`, default model
  **Claude Haiku 5.5** (`claude-haiku-5-5`). Bedrock remains the default; AWS is still used for SES email and
  the CloudWatch heartbeat.
- Cost per check is computed in the add-on per model and sent as `cost_usd`; the Splunk app uses it.
- `eval/replay.py --provider/--model` to compare models on the same frames.
- Same 33 frames from a good print: Haiku 4.5 (Bedrock) 2 false alarms and the part judged "hidden" in 24;
  **Haiku 5.5 0 false alarms, part seen in 29**, at ~$0.00045 per check (~8× cheaper).

## 0.7.0
- The model now also sees **what the part should look like**: the slicer thumbnail embedded in the G-code,
  fetched once per job from PrusaLink (`/api/v1/job` → `file.refs.thumbnail`), converted with ffmpeg
  (PNG or QOI from `.bgcode`, transparent background flattened onto grey) and sent as an "EXPECTED" image
  with the file name. About 150 extra input tokens per check. Option `use_gcode_thumbnail` (on).
- Samples store the expected image too; the labelling panel shows it.
- `eval/replay.py`: variant `v2-noexp` re-runs without the thumbnail, to measure its effect.

## 0.6.0
- Labelled test set: every warning/failure check (and 1 in `dataset_ok_every` normal checks) is saved to
  `/share/prusa_watch/dataset/<id>/` with both frames, the exact context the model saw and its verdict.
- **"Prusa Watch" panel in the Home Assistant sidebar** (ingress) to label samples: ✅ OK print / ❌ Real failure + issue.
- Quick labels from `input_button` helpers: *Correct* / *False alarm* (latest alert) and *Missed failure*
  (saves and labels the latest frame as a real failure).
- `eval/replay.py --dataset` reports a confusion matrix, recall (failures caught), precision and false-alarm
  rate; variant `recorded` scores the live verdicts without any API calls.

## 0.5.0
- Smart-plug energy per print: set `power_entity` to the plug's power sensor (W) and `energy_price`
  (per kWh). The add-on integrates power over the job and reports kWh and cost in the "Print finished"
  email, `sensor.prusa_watch` (`power_w`, `print_energy_kwh`, `print_energy_cost`) and Splunk (`type=energy`).
- Plug power is included in the telemetry the model sees.
- Example automation `ha/prusa-power-off-after-print.yaml`: plug off after the print once the nozzle is < 50 °C.

## 0.4.1
- "Printer unreachable" is reported once per outage instead of every minute (a switched-off printer is normal).

## 0.4.0
- Heartbeat every `heartbeat_min` minutes to CloudWatch (`PrusaWatch/Heartbeat`) and Splunk, for a
  dead man's switch alarm outside your home network (`aws/heartbeat-alarm.ps1`).
- "Watchdog blind" email when a print runs but no AI check has succeeded for `blind_alert_min` minutes.

## 0.3.1
- `sensor.prusa_watch` carries printer telemetry as attributes (progress, time remaining, nozzle/bed
  temperatures and targets, Z) for dashboards and the SenseCAP Indicator page.

## 0.3.0
- New prompt that understands bed-slinger motion and requires positive visual evidence for a failure.
- Model also reports `part_visible` and `evidence`; guard rule: `detached_part` only counts when the part
  is fully visible.
- A failure verdict is re-checked on a second frame a few seconds later (`confirm_failures`).
- Reference frame is reset after a pause / attention.
- Replay of 33 previously flagged frames from a good print: false alarms 14 → 2.

## 0.2.0
- Optional Splunk HTTP Event Collector output (checks, state changes, emails, errors, token usage).

## 0.1.1
- PrusaLink API-key login, camera rotation option, trimmed secrets.

## 0.1.0
- First release: PrusaLink telemetry + RTSP frame → Claude Haiku 4.5 on Amazon Bedrock → SES email.
