#!/usr/bin/with-contenv bashio
bashio::log.info "Starting Prusa Watch"
exec python3 -m prusa_watch --config /data/options.json
