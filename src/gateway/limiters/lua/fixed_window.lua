-- p1 = limit (requests per window), p2 = window length (ms)
algos.fixed_window = function(key, now, cost, limit, window)
  local start = math.floor(now / window) * window
  local state = redis.call('HMGET', key, 'start', 'count')
  local count = 0
  if tonumber(state[1]) == start then
    count = tonumber(state[2]) or 0
  end

  local reset = math.max(1, math.ceil(start + window - now))
  if count + cost <= limit then
    local write = function()
      redis.call('HSET', key, 'start', fmt(start), 'count', count + cost)
      redis.call('PEXPIRE', key, reset + 1000)
    end
    return {1, limit - count - cost, 0, reset, 0}, write
  end
  return {0, math.max(0, limit - count), reset, reset, 0}, nil
end
