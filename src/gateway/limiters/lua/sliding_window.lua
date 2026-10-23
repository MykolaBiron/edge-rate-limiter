-- Sliding window counter: O(1) memory approximation that weights the previous
-- fixed window by how much of it still overlaps the sliding window.
-- p1 = limit, p2 = window length (ms)
algos.sliding_window = function(key, now, cost, limit, window)
  local start = math.floor(now / window) * window
  local state = redis.call('HMGET', key, 'start', 'cur', 'prev')
  local stored = tonumber(state[1])
  local cur, prev = 0, 0
  if stored == start then
    cur = tonumber(state[2]) or 0
    prev = tonumber(state[3]) or 0
  elseif stored == start - window then
    prev = tonumber(state[2]) or 0
  end

  local elapsed = now - start
  local estimate = prev * (window - elapsed) / window + cur

  if estimate + cost <= limit + EPS then
    local new_cur = cur + cost
    local write = function()
      redis.call('HSET', key, 'start', fmt(start), 'cur', new_cur, 'prev', prev)
      redis.call('PEXPIRE', key, 2 * window + 1000)
    end
    local remaining = math.max(0, math.floor(limit - estimate - cost + EPS))
    return {1, remaining, 0, math.ceil(start + 2 * window - now), 0}, write
  end

  -- Solve for the earliest time the estimate drops to (limit - cost).
  local target = limit - cost
  local retry
  if cur <= target then
    -- Still in this window: prev * (window - x) / window + cur <= target
    retry = start + window - (target - cur) * window / prev - now
  else
    -- Next window, where cur becomes prev: cur * (window - x) / window <= target
    retry = start + window + (window - target * window / cur) - now
  end
  local reset = cur > 0 and (start + 2 * window - now) or (start + window - now)
  local remaining = math.max(0, math.floor(limit - estimate + EPS))
  return {0, remaining, math.max(1, math.ceil(retry)), math.ceil(reset), 0}, nil
end
