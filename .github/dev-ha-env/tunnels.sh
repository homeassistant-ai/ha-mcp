# Quick tunnels for dev-ha-env.yml, sourced by its boot and keep-running steps.
#
# A quick tunnel gets a new URL every time it starts, so a tunnel that fails
# to start is retried and one that dies is restarted; either way the URLs are
# encrypted to the run's public key again: the workflow uploads them as the
# dev-ha-env-urls artifact, and each publish also prints a DEVENV_URLS line.
# State lives in tunnel-<name>.{target,pid,url,log} beside the workflow.

# Start the <name> tunnel to <target>, retrying; return 1 if every attempt failed.
start_tunnel() {
  local name=$1 target=$2 log="tunnel-$1.log" pid url attempt i
  echo "$target" > "tunnel-$name.target"
  rm -f "tunnel-$name.url"
  for attempt in 1 2 3 4 5; do
    nohup ./cloudflared tunnel --no-autoupdate --url "$target" > "$log" 2>&1 &
    pid=$!
    echo "$pid" > "tunnel-$name.pid"
    url=""
    for i in $(seq 1 30); do
      # cloudflared names api.trycloudflare.com when it fails to get a quick
      # tunnel; that host is never ours.
      url=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$log" \
        | grep -v '^https://api\.' | head -1)
      [ -n "$url" ] && break
      kill -0 "$pid" 2>/dev/null || break
      sleep 2
    done
    if [ -n "$url" ]; then
      echo "::add-mask::$url"
      echo "::add-mask::${url#https://}"
      echo "$url" > "tunnel-$name.url"
      return 0
    fi
    kill "$pid" 2>/dev/null || true
    echo "::warning::The $name tunnel did not start (attempt $attempt); retrying."
    tail -5 "$log"
    sleep $((attempt * 20))
  done
  return 1
}

# Encrypt the current URLs to out/dev-ha-env-urls.enc and print them as a
# DEVENV_URLS line. A tunnel that is down shows as "down".
publish_urls() {
  local ha mcp
  ha=$(cat tunnel-ha.url 2>/dev/null || echo down)
  mcp=$(cat tunnel-mcp.url 2>/dev/null || echo down)
  [ "$mcp" = down ] || mcp="$mcp$(cat mcp-path)"
  mkdir -p out
  printf 'HA:  %s\nMCP: %s\n' "$ha" "$mcp" \
    | openssl pkeyutl -encrypt -pubin -inkey "$RUNNER_TEMP/url-key.pem" \
      -pkeyopt rsa_padding_mode:oaep -out out/dev-ha-env-urls.enc
  echo "DEVENV_URLS $(openssl base64 -A -in out/dev-ha-env-urls.enc)"
}

# Restart every tunnel whose process has exited; publish once and set REVIVED
# if any came back.
revive_tunnels() {
  local name revived=""
  for name in ha mcp; do
    if ! kill -0 "$(cat "tunnel-$name.pid" 2>/dev/null)" 2>/dev/null \
      || [ ! -s "tunnel-$name.url" ]; then
      echo "::warning::The $name tunnel is down; restarting it, so its URL changes."
      start_tunnel "$name" "$(cat "tunnel-$name.target")" && revived=1
    fi
  done
  [ -z "$revived" ] || { publish_urls; REVIVED=1; }
}

# Watch the holder and revive tunnels. After a tunnel comes back with a new
# URL, return with revived=true so the workflow uploads the URLs artifact
# again; with "last", keep going and leave the new URLs in the job log only.
keep_running() {
  while kill -0 "$(cat hold.pid)" 2>/dev/null; do
    sleep 60
    grep -E "STATUS|Traceback|Error" env.log | tail -3 || true
    REVIVED=""
    revive_tunnels
    if [ -n "$REVIVED" ] && [ "${1:-}" != last ]; then
      echo "revived=true" >> "$GITHUB_OUTPUT"
      return 0
    fi
  done
  tail -80 env.log
}
