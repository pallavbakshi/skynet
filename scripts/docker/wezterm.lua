-- WezTerm mux-server config for AGP runtime containers.
-- Headless mode: no GUI, no keybindings, no fonts — just the multiplexer.
local wezterm = require 'wezterm'
local config = wezterm.config_builder()

--------------------------------------------------------------------------------
-- Terminal size — large defaults for TUI apps (Claude Code, Codex).
-- These set the initial size for new panes spawned via `wezterm cli spawn`.
--------------------------------------------------------------------------------
config.initial_cols = 200
config.initial_rows = 50

--------------------------------------------------------------------------------
-- Scrollback — generous buffer for long-running agent sessions.
-- Matches the AGP WezTermHost default (scrollback_lines=5000) with headroom.
--------------------------------------------------------------------------------
config.scrollback_lines = 10000

--------------------------------------------------------------------------------
-- No GUI chrome — mux-server is headless.
--------------------------------------------------------------------------------
config.enable_tab_bar = false
config.window_decorations = "NONE"
config.window_padding = { left = 0, right = 0, top = 0, bottom = 0 }

--------------------------------------------------------------------------------
-- Term type — ensure TUI apps get proper capabilities.
--------------------------------------------------------------------------------
config.term = "xterm-256color"

--------------------------------------------------------------------------------
-- Performance — disable features that are irrelevant headless.
--------------------------------------------------------------------------------
config.animation_fps = 1
config.max_fps = 10
config.audible_bell = "Disabled"

--------------------------------------------------------------------------------
-- Unix domain for local mux-server (wezterm-mux-server --daemonize).
--------------------------------------------------------------------------------
config.unix_domains = {
  { name = "mux" },
}

return config
