-- Shared by every algorithm. Each algorithm registers a function in `algos`:
--   algos.<name>(key, now_ms, cost, p1, p2) -> result, write
-- result = {allowed, remaining, retry_after_ms, reset_ms, delay_ms}
-- write  = closure that persists the new state (nil when nothing to persist).
-- The dispatcher only calls the writes when every component allows, which is
-- what makes composite limits all-or-nothing.
local algos = {}

-- Lua's default number->string conversion keeps 14 significant digits, which
-- drops sub-millisecond precision from epoch-ms timestamps.
local function fmt(x)
  return string.format('%.3f', x)
end

local EPS = 1e-9
