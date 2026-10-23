-- p1 = capacity (tokens), p2 = refill rate (tokens/second)
algos.token_bucket = function(key, now, cost, capacity, rate)
  local state = redis.call('HMGET', key, 'tokens', 'ts')
  local tokens = tonumber(state[1])
  local ts = tonumber(state[2])
  if tokens == nil or ts == nil then
    tokens = capacity
    ts = now
  end
  -- Never move time backwards (e.g. a caller-supplied clock or a failover).
  if now > ts then
    tokens = math.min(capacity, tokens + (now - ts) * rate / 1000)
    ts = now
  end

  local ms_per_token = 1000 / rate
  if tokens + EPS >= cost then
    local left = math.max(0, tokens - cost)
    local write = function()
      redis.call('HSET', key, 'tokens', fmt(left), 'ts', fmt(ts))
      redis.call('PEXPIRE', key, math.ceil(capacity * ms_per_token) + 1000)
    end
    return {1, math.floor(left + EPS), 0, math.ceil((capacity - left) * ms_per_token), 0}, write
  end

  local retry = math.max(1, math.ceil((cost - tokens) * ms_per_token))
  return {0, math.floor(tokens + EPS), retry, math.ceil((capacity - tokens) * ms_per_token), 0}, nil
end
