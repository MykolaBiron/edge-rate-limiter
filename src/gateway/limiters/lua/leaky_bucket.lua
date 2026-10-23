-- Leaky bucket as a queue: requests drain at a constant rate and at most
-- `capacity` may be queued. The state is the virtual time at which the queue
-- empties. delay_ms tells a shaping gateway how long to hold the request so
-- the upstream sees a smooth rate instead of a burst.
-- p1 = capacity (queue size), p2 = leak rate (requests/second)
algos.leaky_bucket = function(key, now, cost, capacity, rate)
  local interval = 1000 / rate
  local horizon = capacity * interval
  local free_at = tonumber(redis.call('GET', key))
  if free_at == nil or free_at < now then
    free_at = now
  end

  local backlog = free_at - now
  local new_backlog = backlog + cost * interval
  if new_backlog <= horizon + EPS then
    local write = function()
      redis.call('SET', key, fmt(now + new_backlog), 'PX', math.ceil(new_backlog) + 1000)
    end
    local remaining = math.floor((horizon - new_backlog) / interval + EPS)
    return {1, remaining, 0, math.ceil(new_backlog), math.floor(backlog)}, write
  end

  local remaining = math.max(0, math.floor((horizon - backlog) / interval + EPS))
  local retry = math.max(1, math.ceil(new_backlog - horizon))
  return {0, remaining, retry, math.ceil(backlog), 0}, nil
end
