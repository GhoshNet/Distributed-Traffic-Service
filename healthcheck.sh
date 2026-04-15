#! bin/bash

for port in 8001 8002 8003 8004 8005 8006; do
  echo -n "Port $port: "; curl -s http://localhost:$port/health | python3 -m json.tool 2>/dev/null | grep
 '"status"'
done
