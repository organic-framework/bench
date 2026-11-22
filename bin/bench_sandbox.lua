-- Bench's closed Lua environment (docs/bench.md, "The reader"). bin/bench.py runs this file in a Lua process of
-- its own, fed on stdin after one line `local REQ = <data>`: the package's Lua sources, the words each file may use,
-- and what to do. The process is given no file: it reads its program from stdin and writes JSON to stdout.
--
-- What a file evaluated here is given: the words (package, module, setting, ...), a `require` that sees only the
-- sources it was handed, and a short list of the standard library that reaches nothing outside the process. Not
-- given: io, os, load, loadstring, dofile, require of a library, package, debug, collectgarbage, getfenv, setfenv,
-- string.dump, LuaJIT's ffi and jit. A count hook ends the process on its instruction budget and on its memory
-- budget, and the caller ends it on wall time.
--
-- Runs on Lua 5.1, 5.4 and LuaJIT 2.1.

-- ---- what the runner keeps for itself, before any file runs ----
local type, next, pairs, ipairs, select, error, pcall = type, next, pairs, ipairs, select, error, pcall
local rawget, rawset, rawequal, setmetatable, getmetatable = rawget, rawset, rawequal, setmetatable, getmetatable
local tostring, tonumber, assert, xpcall = tostring, tonumber, assert, xpcall
local unpack = rawget(_G, "unpack") or table.unpack
local s_byte, s_char, s_find, s_format, s_gmatch = string.byte, string.char, string.find, string.format, string.gmatch
local s_gsub, s_len, s_lower, s_match, s_rep = string.gsub, string.len, string.lower, string.match, string.rep
local s_reverse, s_sub, s_upper = string.reverse, string.sub, string.upper
local t_concat, t_insert, t_remove, t_sort = table.concat, table.insert, table.remove, table.sort
local m_floor, m_huge = math.floor, math.huge
local out, err = io.stdout, io.stderr
local exit = os.exit
local sethook = debug.sethook
local gc = collectgarbage
local load, loadstring, setfenv = load, rawget(_G, "loadstring"), rawget(_G, "setfenv")
local co_create, co_resume, co_yield = coroutine.create, coroutine.resume, coroutine.yield
local co_status, co_running = coroutine.status, coroutine.running
local co_isyieldable = rawget(coroutine, "isyieldable")
local jit = rawget(_G, "jit")
local LUA51 = rawget(_G, "_VERSION") == "Lua 5.1" and not jit

local LIMITS = REQ.limits
local MAX_INSTR = LIMITS.instructions
local MAX_KB = LIMITS.memory_kb
local MAX_STR = LIMITS.string_bytes
local MAX_OUT = LIMITS.output_bytes

local function stop(reason)   -- nothing a file does catches this: the process ends here
  err:write("bench-sandbox: stopped: ", reason, "\n")
  exit(3)
end

-- ---- the budgets ----
-- LuaJIT calls no hook in compiled code, so its compiler is off for the whole process.
if jit then jit.off(); jit.flush() end

local count = 0
local function hook()
  count = count + 1
  if count > MAX_INSTR then stop("instructions") end
  if gc("count") > MAX_KB then stop("memory") end
end

-- ---- the standard library a file is given ----
local function cap(n, what)
  if n > MAX_STR then error(what .. ": a string over " .. MAX_STR .. " bytes", 3) end
end

local safe_string = {
  byte = s_byte, char = s_char, find = s_find, format = s_format, gmatch = s_gmatch, len = s_len, lower = s_lower,
  match = s_match, reverse = s_reverse, sub = s_sub, upper = s_upper,
}
function safe_string.rep(s, n, sep)
  n = tonumber(n)   -- Lua takes a count written as text too; cap what it will be, never the text
  if n == nil then error("string.rep: a count is a number", 2) end
  local one = s_len(tostring(s)) + (sep and s_len(tostring(sep)) or 0)
  if n > 0 then cap(one * n, "string.rep") end
  if sep == nil then return s_rep(s, n) end
  return s_rep(s, n, sep)
end
function safe_string.gsub(s, pattern, repl, n)
  s = tostring(s)
  if type(repl) == "string" then
    cap(s_len(s) * (s_len(repl) + 1) + s_len(repl), "string.gsub")
    return s_gsub(s, pattern, repl, n)
  end
  local total = 0
  local function counted(v)
    if type(v) == "string" or type(v) == "number" then
      total = total + s_len(tostring(v))
      cap(total, "string.gsub")
    end
    return v
  end
  if type(repl) == "function" then
    return s_gsub(s, pattern, function(...) return counted(repl(...)) end, n)
  end
  if type(repl) == "table" then
    return s_gsub(s, pattern, function(k) return counted(repl[k]) end, n)
  end
  return s_gsub(s, pattern, repl, n)
end

-- string.format: what the result can hold, before it is built: the format, each argument written out (a string
-- under %q up to four times its length), and a width or precision of up to 99 per directive
function safe_string.format(f, ...)
  f = tostring(f)
  local total = s_len(f)
  local _, directives = s_gsub(f, "%%", "")
  total = total + directives * 100
  -- any other argument is written out once, here, and counted: a __tostring could give a long text at each %s
  local args, n = {}, select("#", ...)
  for i = 1, n do
    local v = select(i, ...)
    if type(v) ~= "number" and type(v) ~= "string" then v = tostring(v) end
    args[i] = v
    total = total + (type(v) == "string" and 4 * s_len(v) or 32)
    cap(total, "string.format")
  end
  return s_format(f, unpack(args, 1, n))
end

local safe_table = { insert = t_insert, remove = t_remove, sort = t_sort, unpack = unpack }
function safe_table.concat(t, sep, i, j)
  -- each value is read once, into a plain list, and that list is joined: what was counted is what is joined
  -- (an __index could give a short value to the count and a long one to the join)
  local total, sl, list = 0, sep and s_len(tostring(sep)) or 0, {}
  for k = i or 1, j or #t do
    local v = t[k]
    if type(v) == "string" or type(v) == "number" then total = total + s_len(tostring(v)) + sl end
    cap(total, "table.concat")
    list[#list + 1] = v
  end
  return t_concat(list, sep)
end

local safe_math = {}
for k, v in pairs(math) do safe_math[k] = v end

local function new_thread(f)
  local co = co_create(f)
  sethook(co, hook, "", 1)   -- PUC Lua copies the hook into a new thread too; LuaJIT's hook is the whole VM's
  return co
end
local safe_coroutine = {
  create = new_thread, resume = co_resume, yield = co_yield, status = co_status, running = co_running,
  isyieldable = co_isyieldable,
}
function safe_coroutine.wrap(f)
  local co = new_thread(f)
  return function(...)
    local r = { co_resume(co, ...) }
    if not r[1] then error(r[2], 2) end
    return select(2, unpack(r))
  end
end

local function safe_getmetatable(x)
  if type(x) == "string" then return nil end   -- the string library is the runner's
  return getmetatable(x)
end

-- strings' methods are the safe ones too: ("x"):rep(n) is string.rep
getmetatable("").__index = safe_string

-- ---- the words ----
local DECLS = {}            -- every declaration, in order
local VALUE_META              -- a value word's result: callable once more with a table
VALUE_META = {
  __metatable = false,
  __call = function(self, body)
    if rawget(self, "body") ~= nil then error(self["@"] .. ": given a table twice", 2) end
    return setmetatable({ ["@"] = self["@"], args = self.args, body = body }, VALUE_META)
  end,
}

local function value_word(w)
  return function(...)
    return setmetatable({ ["@"] = w, args = { ... } }, VALUE_META)
  end
end

local function decl_word(w)
  return function(a, b, extra)
    if extra ~= nil then error(w .. ": takes a name and a table, no more", 2) end
    local d = { word = w }
    DECLS[#DECLS + 1] = d
    if type(a) == "table" and b == nil then d.body = a; return nil end
    if type(a) ~= "string" then error(w .. ": takes a name or a table", 2) end
    d.name = a
    if b ~= nil then
      if type(b) ~= "table" then error(w .. " \"" .. a .. "\": the second argument is a table", 2) end
      d.body = b
      return nil
    end
    return function(body, more)
      if more ~= nil or type(body) ~= "table" then error(w .. " \"" .. a .. "\": takes one table", 2) end
      if d.body ~= nil then error(w .. " \"" .. a .. "\": given a table twice", 2) end
      d.body = body
    end
  end
end

-- ---- compiling a source with the closed environment ----
local FILES = REQ.files
local ENV = {}
local loaded = {}

local function compile(src, name)
  if type(src) ~= "string" then error("no source " .. name, 0) end
  if s_byte(src, 1) == 27 then error(name .. ": a compiled chunk, not Lua source", 0) end
  local fn, msg
  if LUA51 then
    fn, msg = loadstring(src, "=" .. name)
    if fn then setfenv(fn, ENV) end
  else
    fn, msg = load(src, "=" .. name, "t", ENV)
  end
  if not fn then error(msg, 0) end
  return fn
end

local function sandbox_require(name)
  if type(name) ~= "string" or not s_match(name, "^[%w_][%w_%-]*[%.%w_%-]*$") or s_find(name, "..", 1, true)
     or s_sub(name, -1) == "." then
    error("require: \"" .. (type(name) == "string" and name or type(name)) .. "\" is not a module of this package", 2)
  end
  local path = s_gsub(name, "%.", "/") .. ".lua"
  local have = rawget(loaded, path)
  if have == false then error("require: \"" .. name .. "\" requires itself", 2) end
  if have ~= nil then return have end
  if rawget(FILES, path) == nil then error("require: no " .. path .. " in this package", 2) end
  rawset(loaded, path, false)
  local r = compile(rawget(FILES, path), path)()
  if r == nil then r = true end
  rawset(loaded, path, r)
  return r
end

-- setmetatable without finalizers: Lua 5.4 runs a __gc with every hook off, so a finalizer would spend instructions
-- and memory outside the budgets (5.4 marks an object for finalizing only when its metatable holds __gc then)
local function safe_setmetatable(t, mt)
  if type(mt) == "table" and rawget(mt, "__gc") ~= nil then error("setmetatable: __gc is not given here", 2) end
  return setmetatable(t, mt)
end

local GIVEN = {
  assert = assert, error = error, ipairs = ipairs, next = next, pairs = pairs, pcall = pcall, select = select,
  tonumber = tonumber, tostring = tostring, type = type, xpcall = xpcall, unpack = unpack,
  rawequal = rawequal, rawget = rawget, rawset = rawset, rawlen = rawget(_G, "rawlen"),
  setmetatable = safe_setmetatable, getmetatable = safe_getmetatable,
  string = safe_string, table = safe_table, math = safe_math, coroutine = safe_coroutine,
  require = sandbox_require,
}
for k, v in pairs(GIVEN) do ENV[k] = v end
for _, w in ipairs(REQ.words.decl or {}) do ENV[w] = decl_word(w) end
for _, w in ipairs(REQ.words.value or {}) do ENV[w] = value_word(w) end
setmetatable(ENV, {
  __metatable = false,
  __index = function(_, k)
    error("unknown name \"" .. (type(k) == "string" and k or type(k)) .. "\": not a word of this file", 2)
  end,
})

-- ---- JSON out ----
local parts, size = {}, 0
local function emit(s)
  size = size + s_len(s)
  if size > MAX_OUT then error("the result is over " .. MAX_OUT .. " bytes", 0) end
  parts[#parts + 1] = s
end

local ESC = { ['"'] = '\\"', ['\\'] = '\\\\', ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t' }
local function json_string(s)
  return '"' .. s_gsub(s, '[%c"\\]', function(c)
    return ESC[c] or s_format("\\u%04x", s_byte(c))
  end) .. '"'
end

local function is_array(t)
  local n = 0
  for k in next, t do
    if type(k) ~= "number" or k < 1 or k ~= m_floor(k) then return false end
    n = n + 1
  end
  for i = 1, n do if rawget(t, i) == nil then return false end end
  return true, n
end

local seen = {}
local function encode(v, path, depth)
  local tv = type(v)
  if depth > 64 then error("the result is nested deeper than 64", 0) end
  if v == nil then emit("null")
  elseif tv == "boolean" then emit(v and "true" or "false")
  elseif tv == "number" then
    if v ~= v or v == m_huge or v == -m_huge then error("a number that is not finite at " .. t_concat(path, "."), 0) end
    if v == m_floor(v) and v > -2^53 and v < 2^53 then emit(s_format("%d", v)) else emit(s_format("%.17g", v)) end
  elseif tv == "string" then emit(json_string(v))
  elseif tv == "function" then
    -- kept, not called: its place, so a later call finds it again
    emit('{"@":"function","path":[')
    for i, p in ipairs(path) do
      if i > 1 then emit(",") end
      if type(p) == "number" then emit(s_format("%d", p)) else emit(json_string(p)) end
    end
    emit("]}")
  elseif tv == "table" then
    if seen[v] then error("the result holds a table inside itself at " .. t_concat(path, "."), 0) end
    seen[v] = true
    local arr, n = is_array(v)
    if arr and n > 0 then
      emit("[")
      for i = 1, n do
        if i > 1 then emit(",") end
        path[#path + 1] = i
        encode(rawget(v, i), path, depth + 1)
        path[#path] = nil
      end
      emit("]")
    else
      emit("{")
      local keys, named = {}, {}
      for k in next, v do
        local name = type(k) == "number" and k == m_floor(k) and s_format("%d", k) or k
        if type(name) == "string" and named[name] then
          error("the keys " .. name .. " and \"" .. name .. "\" are one key once written out, at " .. t_concat(path, "."), 0)
        end
        if type(name) == "string" then named[name] = true end
        if type(k) == "string" then keys[#keys + 1] = k
        elseif type(k) == "number" and k == m_floor(k) then keys[#keys + 1] = k
        else error("a key that is neither a string nor a whole number at " .. t_concat(path, "."), 0) end
      end
      t_sort(keys, function(a, b)
        if type(a) == type(b) then return a < b end
        return type(a) == "number"
      end)
      for i, k in ipairs(keys) do
        if i > 1 then emit(",") end
        emit(json_string(type(k) == "number" and s_format("%d", k) or k))
        emit(":")
        path[#path + 1] = k
        encode(rawget(v, k), path, depth + 1)
        path[#path] = nil
      end
      emit("}")
    end
    seen[v] = nil
  else
    error("a " .. tv .. " cannot leave the reader, at " .. t_concat(path, "."), 0)
  end
end

local function message(e)
  if type(e) == "string" then return e end
  return "an error that is not a string (" .. type(e) .. ")"
end

-- ---- the run ----
local function evaluate()
  local returned = compile(rawget(FILES, REQ.entry), REQ.entry)()
  if REQ.mode == "read" then
    return { decls = DECLS, returned = returned }
  end
  -- call: the function kept at PATH (the marker an earlier read gave), with ARGS
  local node = { decls = DECLS, returned = returned }
  for _, p in ipairs(REQ.path) do
    if type(node) ~= "table" then error("no function at that place", 0) end
    node = rawget(node, p)
  end
  if type(node) ~= "function" then error("no function at that place", 0) end
  return { value = node(unpack(REQ.args, 1, REQ.nargs)) }
end

sethook(hook, "", 1)
local ok, result = pcall(evaluate)
if ok then
  ok, result = pcall(function()
    emit('{"ok":true,"result":')
    encode(result, {}, 0)
    emit("}")
  end)
end
sethook()
if not ok then
  parts, size = {}, 0
  emit('{"ok":false,"error":' .. json_string(s_sub(message(result), 1, 2000)) .. "}")
end
out:write(t_concat(parts))
out:flush()
exit(0)
