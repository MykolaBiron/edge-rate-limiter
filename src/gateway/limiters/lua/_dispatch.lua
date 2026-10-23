-- KEYS[i]  = state key of component i
-- ARGV[1]  = now in ms ('' = use the Redis server clock, so replicas never skew)
-- ARGV[2]  = cost
-- ARGV[3]  = number of components n
-- ARGV[4 + 3(i-1) ..] = algorithm name, p1, p2 for component i
-- Returns {allowed, remaining, retry_after_ms, reset_ms, delay_ms, binding}
-- where binding is the 1-based component that decided the outcome.
local now
if ARGV[1] ~= '' then
  now = tonumber(ARGV[1])
else
  local t = redis.call('TIME')
  now = tonumber(t[1]) * 1000 + tonumber(t[2]) / 1000
end
local cost = tonumber(ARGV[2])
local n = tonumber(ARGV[3])

local allowed, remaining, retry, reset, delay = 1, nil, 0, 0, 0
local binding = 1
local writes = {}

for i = 1, n do
  local base = 4 + (i - 1) * 3
  local fn = algos[ARGV[base]]
  if fn == nil then
    return redis.error_reply('unknown algorithm: ' .. tostring(ARGV[base]))
  end
  local res, write = fn(KEYS[i], now, cost, tonumber(ARGV[base + 1]), tonumber(ARGV[base + 2]))

  if res[1] == 0 then
    -- Report the component that will take longest to admit the request.
    if allowed == 1 or res[3] > retry then
      binding = i
      retry = res[3]
    end
    allowed = 0
  elseif allowed == 1 and (remaining == nil or res[2] < remaining) then
    binding = i
  end
  if remaining == nil or res[2] < remaining then
    remaining = res[2]
  end
  reset = math.max(reset, res[4])
  delay = math.max(delay, res[5])
  writes[i] = write
end

if allowed == 1 then
  for i = 1, n do
    if writes[i] then writes[i]() end
  end
else
  delay = 0
end
return {allowed, remaining, retry, reset, delay, binding}
