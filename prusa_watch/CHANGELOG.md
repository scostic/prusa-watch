# Changelog

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
