-- Sliding window log: exact, one sorted-set entry per admitted request.
-- p1 = limit, p2 = window length (ms)
algos.sliding_log = function(key, now, cost, limit, window)
  -- Pruning expired entries is safe to do even if the request is rejected.
  redis.call('ZREMRANGEBYSCORE', key, '-inf', fmt(now - window))
  local count = redis.call('ZCARD', key)

  if count + cost <= limit then
    local write = function()
      for i = 1, cost do
        redis.call('ZADD', key, fmt(now), fmt(now) .. ':' .. (count + i))
      end
      redis.call('PEXPIRE', key, math.ceil(window) + 1000)
    end
    return {1, limit - count - cost, 0, math.ceil(window), 0}, write
  end

  -- The oldest (count + cost - limit) entries must expire before this fits.
  local idx = count + cost - limit - 1
  local entry = redis.call('ZRANGE', key, idx, idx, 'WITHSCORES')
  local retry = math.max(1, math.ceil(tonumber(entry[2]) + window - now))
  local newest = redis.call('ZRANGE', key, -1, -1, 'WITHSCORES')
  local reset = math.ceil(tonumber(newest[2]) + window - now)
  return {0, math.max(0, limit - count), retry, reset, 0}, nil
end
