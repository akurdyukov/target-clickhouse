#!/bin/sh
# Start ClickHouse for the tests: http on localhost:18123, native on localhost:19000.
# Pick the server version with CLICKHOUSE_VERSION (e.g. CLICKHOUSE_VERSION=23.4);
# any extra arguments are passed to `docker run`.
#
# CLICKHOUSE_SKIP_USER_SETUP=1 keeps the passwordless `default` user reachable
# over the network, which newer images otherwise restrict to localhost.
docker run -p 18123:8123 -p 19000:9000 --rm --name clickhouse-server \
  --ulimit nofile=262144:262144 \
  -e CLICKHOUSE_SKIP_USER_SETUP=1 \
  "$@" \
  "clickhouse/clickhouse-server:${CLICKHOUSE_VERSION:-26.8}-alpine"
