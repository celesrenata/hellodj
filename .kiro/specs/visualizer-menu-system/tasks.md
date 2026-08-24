# Implementation Plan: Visualizer Menu System

## Overview

Implement a client-side UI overlay for the HelloDJ Activity frontend that allows viewers to browse engines, apply presets, and adjust engine-specific settings — all synchronized in real-time via WebSocket. The backend provides structured data endpoints through the existing WS hub, and the frontend renders a glassmorphism slide-in panel with engine selector, preset browser, and settings panel subviews.

## Tasks

- [x] 1. Backend data layer — engine metadata, setting groups, schema enrichment
  - [x] 1.1 Create `bot/video/visualizer_engines/engine_metadata.py` with ENGINE_METADATA dict and SETTING_GROUPS dict
    - Define all 6 engine entries (projectm, audiovis, fosfora, varda, drift, dvd) with name, description (≤60 chars), icon, and server_rendered fields
    - Define SETTING_GROUPS mapping for each engine grouping settings into labeled sections
    - Export helper `get_engine_metadata(engine_id)` and `get_setting_group(engine_id, setting_key)`
    - _Requirements: 2.1, 2.2, 4.6, 10.1_

  - [x] 1.2 Enrich config schema with label, group, and current-value resolution
    - Add a `build_settings_schema(engine_id, guild_config)` function in engine_metadata.py that merges ENGINE_CONFIG_SCHEMAS entries with SETTING_GROUPS labels and guild's current values (or defaults)
    - Each entry returns: setting, type, label, default, current, min, max, group
    - _Requirements: 4.1, 4.2, 4.6, 10.3_

  - [x] 1.3 Add `visualizer_presets` key support in guild_settings.py
    - Implement `get_user_presets(guild_id, engine)`, `save_user_preset(guild_id, name, engine, config)`, `delete_user_preset(guild_id, name)` methods
    - Validate preset name format (1–50 chars, `^[a-zA-Z0-9 -]{1,50}$`)
    - Persist under `visualizer_presets` in guild settings JSON
    - _Requirements: 6.2, 6.3, 6.4, 6.5_

  - [x] 1.4 Add preset tag generation utility
    - Implement `generate_preset_tags(config: dict) -> list[str]` returning up to 4 metadata tags derived from config key-value pairs
    - _Requirements: 3.2_

- [x] 2. Backend WS handlers — menu message routing and validation
  - [x] 2.1 Implement `_handle_menu_init` in ws_hub.py
    - Respond with all engines from ENGINE_METADATA, active engine from guild settings, active preset name (if any)
    - Return `menu_init_response` message to the requesting client
    - _Requirements: 5.1, 10.1_

  - [x] 2.2 Implement `_handle_presets_list` in ws_hub.py
    - Accept `{engine: string}`, validate engine exists
    - Return factory presets from factory_presets.py filtered by engine + user presets from guild settings
    - Include generated tags per preset
    - Return `presets_list_response` with factory_presets and user_presets arrays
    - _Requirements: 3.1, 10.2_

  - [x] 2.3 Implement `_handle_settings_schema` in ws_hub.py
    - Accept `{engine: string}`, validate engine exists
    - Return `settings_schema_response` with enriched schema entries from `build_settings_schema`
    - _Requirements: 4.1, 4.2, 4.6, 10.3_

  - [x] 2.4 Implement `_handle_engine_switch` in ws_hub.py
    - Accept `{engine: string, request_id: string}`, validate engine exists
    - Delegate to VisualizerManager.set_engine(), persist in guild settings
    - Respond with `engine_switch_ack` (success/error), broadcast `visualizer_state` to all clients
    - _Requirements: 2.4, 2.5, 5.3, 10.4_

  - [x] 2.5 Implement `_handle_preset_apply` in ws_hub.py
    - Accept `{preset_name: string, request_id: string}`, resolve preset from factory or user presets
    - Apply config via guild_settings, respond with `preset_apply_ack`, broadcast `visualizer_state`
    - _Requirements: 3.3, 3.4, 5.3, 10.4_

  - [x] 2.6 Implement `_handle_setting_change` in ws_hub.py
    - Accept `{setting: string, value: any, request_id: string}`, validate value against schema (type, min/max)
    - Persist via guild_settings, respond with `setting_change_ack`, broadcast `visualizer_state`
    - _Requirements: 4.3, 4.4, 4.5, 5.3, 10.4_

  - [x] 2.7 Implement `_handle_preset_save` in ws_hub.py
    - Accept `{name: string, request_id: string}`, validate name format
    - Save current engine config as user preset, respond with `preset_save_ack`, broadcast `preset_added`
    - _Requirements: 6.1, 6.2, 6.3, 10.4_

  - [x] 2.8 Implement `_handle_preset_delete` in ws_hub.py
    - Accept `{name: string, request_id: string}`, block deletion of factory presets
    - Delete user preset, respond with `preset_delete_ack`, broadcast `preset_removed`
    - _Requirements: 6.4, 6.5, 10.4_

  - [x] 2.9 Wire menu message types into ws_hub.py `_handle_message` dispatcher
    - Register all 8 new message types (menu_init, presets_list, settings_schema, engine_switch, preset_apply, setting_change, preset_save, preset_delete)
    - Catch unknown types / validation errors and return `{success: false, error: "..."}` responses
    - _Requirements: 10.4, 10.5_

- [x] 3. Checkpoint — Backend validation
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Frontend modules — menu panel, engine selector, preset browser, settings panel, save modal
  - [x] 4.1 Create `bot/video/activity_frontend/menu_panel.js` — VisualizerMenu class
    - Implement constructor(containerEl, wsSend, onClose), open(), close(), destroy() lifecycle
    - Implement showEngines(), showPresets(), showSettings() navigation with horizontal slide transitions (250ms)
    - Implement handleMessage(data) routing for all menu response/broadcast types
    - Implement onVisualizerStateChange(state) for live state sync
    - Send `menu_init` on open, update UI on `menu_init_response`
    - _Requirements: 1.2, 1.3, 5.1, 5.2, 7.2_

  - [x] 4.2 Create `bot/video/activity_frontend/engine_selector.js` — EngineSelector class
    - Implement render(engines, activeEngine) — generate Glass_Panel grid cards with name, icon, description
    - Implement setLoading(engineId), clearLoading(engineId), setActive(engineId), setError(engineId, msg)
    - Emit engine_switch on card click (only if not already active)
    - 10s timeout handling: clear loading, restore previous active, show error indicator
    - Arrow key navigation with visible focus indicators
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 9.5_

  - [x] 4.3 Create `bot/video/activity_frontend/preset_browser.js` — PresetBrowser class
    - Implement render(presets, activePreset) — grouped Factory/User sections with Glass_Panel cards
    - Display preset name + up to 4 metadata tags per card
    - Implement setLoading(presetName), clearLoading(), setActive(presetName), showEmpty()
    - Implement addPreset(preset) and removePreset(presetName) for live updates without full re-render
    - Momentum scrolling for overflow
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7_

  - [x] 4.4 Create `bot/video/activity_frontend/settings_panel.js` — SettingsPanel class
    - Implement render(schema, currentValues) — generate controls by type: slider (float/int), toggle (bool), dropdown (choice)
    - Label each control with setting name + current value display
    - Implement debounce on continuous inputs (100ms settle), send setting_change
    - Implement updateValue(setting, value), revertValue(setting, value), showError(setting, msg) for server confirmation flow
    - Group settings under labeled sections when >4 parameters (use group field from schema)
    - Include "Save Preset" button triggering modal
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 6.1_

  - [x] 4.5 Create `bot/video/activity_frontend/save_preset_modal.js` — SavePresetModal class
    - Implement show(), hide() with Glass_Panel modal styling
    - Focus trap within modal, Escape to close
    - Validate preset name (1–50 chars, alphanumeric + hyphens + spaces)
    - Submit sends preset_save message, inline error on rejection
    - _Requirements: 6.1, 6.2, 9.1_

- [x] 5. Frontend CSS — glassmorphism styles, animations, responsive layout
  - [x] 5.1 Create `bot/video/activity_frontend/menu_styles.css` — menu-specific styles
    - Glass_Panel base: backdrop-filter blur(12px), semi-transparent OKLCH backgrounds, animated border gradients
    - Side panel layout (≥600px): right-anchored, max 360px width, slide-in/out animations
    - Bottom sheet layout (<600px): full width, max 70% viewport height, drag-to-resize (30%–70%)
    - Engine card grid, preset card layout, settings control styling
    - Hover micro-interactions (glow, scale) within 50ms
    - Brand color palette (oklch(0.65 0.25 290) primary) for active states and accent highlights
    - Animated gradient panel header shifting hue per engine
    - Loading indicator and error indicator animations
    - `prefers-reduced-motion` media query: disable animations, use instant transitions
    - Minimum 4.5:1 contrast ratio for text, 3:1 for UI elements
    - Minimum 44×44px touch targets
    - _Requirements: 1.4, 1.5, 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 8.1, 8.2, 8.3, 8.4, 9.4_

- [x] 6. Frontend integration — toggle button, WS routing, app.js hooks
  - [x] 6.1 Add menu toggle button to `bot/video/activity_frontend/index.html`
    - Add button in `.controls-row` alongside existing controls (lyrics, whiteboard, search)
    - Minimum 44×44px touch target, aria-label for accessibility
    - Active state with brand-color glow when menu is open
    - _Requirements: 1.1, 1.4, 1.5, 9.3_

  - [x] 6.2 Wire menu WS message routing in `bot/video/activity_frontend/app.js`
    - Import VisualizerMenu module
    - Register handleMessage for menu-related message types on the existing WebSocket connection
    - Toggle button click → open/close menu with slide animation (open 300ms, close 200ms)
    - Handle `visualizer_state` broadcast → call onVisualizerStateChange on open menu
    - Handle disconnect: disable menu controls, show "Disconnected" indicator; re-sync on reconnect
    - Escape key closes menu, focus returns to toggle button
    - Focus trap within menu while open
    - _Requirements: 1.2, 1.3, 5.2, 5.3, 5.4, 5.5, 9.1, 9.2_

  - [x] 6.3 Add `<link>` for menu_styles.css and `<script type="module">` for menu_panel.js in index.html
    - Ensure proper load order (CSS before JS modules)
    - _Requirements: 1.1_

- [x] 7. Checkpoint — Frontend functional testing
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 8. Property-based tests — backend validation (Hypothesis)
  - [ ]* 8.1 Write property test for preset name validation
    - **Property 8: Preset name validation**
    - Generate random strings, verify acceptance iff matches `^[a-zA-Z0-9 -]{1,50}$`
    - **Validates: Requirements 6.2**

  - [ ]* 8.2 Write property test for factory preset deletion protection
    - **Property 9: Factory preset deletion protection**
    - Generate random preset objects with factory=True/False, verify delete blocked iff factory=true
    - **Validates: Requirements 6.5**

  - [ ]* 8.3 Write property test for menu_init_response completeness
    - **Property 11: menu_init_response completeness**
    - Generate random guild states, verify response contains all engines with valid metadata and correct active_engine
    - **Validates: Requirements 10.1**

  - [ ]* 8.4 Write property test for presets_list_response correctness
    - **Property 12: presets_list_response correctness**
    - Generate random engine + preset combinations, verify factory presets filtered by engine + user presets included
    - **Validates: Requirements 10.2**

  - [ ]* 8.5 Write property test for settings_schema_response fidelity
    - **Property 13: settings_schema_response fidelity**
    - Generate random engines + guild configs, verify schema entries match ENGINE_CONFIG_SCHEMAS with correct current values
    - **Validates: Requirements 10.3**

  - [ ]* 8.6 Write property test for invalid request error responses
    - **Property 14: Invalid request error responses**
    - Generate random invalid inputs (unknown engines, out-of-range values, invalid names), verify ack has success=false and non-empty error
    - **Validates: Requirements 10.5**

- [ ] 9. Property-based tests — frontend validation (fast-check)
  - [ ]* 9.1 Write property test for engine card rendering completeness
    - **Property 1: Engine selector renders complete cards**
    - Generate random engine metadata arrays, verify DOM contains one card per engine with name, icon, and description ≤60 chars
    - **Validates: Requirements 2.1, 2.2**

  - [ ]* 9.2 Write property test for preset tag generation
    - **Property 4: Preset tag generation**
    - Generate random config dicts, verify tags array has ≤4 entries and each corresponds to an actual config key-value
    - **Validates: Requirements 3.2**

  - [ ]* 9.3 Write property test for schema-to-control type mapping
    - **Property 5: Schema-to-control type mapping**
    - Generate random schema entries, verify correct control type: slider for float/int, toggle for bool, dropdown for choice
    - **Validates: Requirements 4.1, 4.2**

  - [ ]* 9.4 Write property test for bottom sheet height clamping
    - **Property 10: Bottom sheet height clamping**
    - Generate random drag position values, verify resulting height clamped between 30% and 70% viewport
    - **Validates: Requirements 8.3**

- [x] 10. Final checkpoint — Integration and wiring
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Backend is Python (Hypothesis for PBT), frontend is vanilla JS ES modules (fast-check for PBT)
- No build step — pure ES modules loaded via `<script type="module">`
- All WS communication uses the existing `ws_hub.py` infrastructure
- Property tests validate universal correctness properties from the design document
- Checkpoints ensure incremental validation between backend and frontend phases

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.4"] },
    { "id": 1, "tasks": ["1.2", "1.3"] },
    { "id": 2, "tasks": ["2.1", "2.2", "2.3", "2.9"] },
    { "id": 3, "tasks": ["2.4", "2.5", "2.6", "2.7", "2.8"] },
    { "id": 4, "tasks": ["4.1", "5.1"] },
    { "id": 5, "tasks": ["4.2", "4.3", "4.4", "4.5"] },
    { "id": 6, "tasks": ["6.1", "6.3"] },
    { "id": 7, "tasks": ["6.2"] },
    { "id": 8, "tasks": ["8.1", "8.2", "8.3", "8.4", "8.5", "8.6"] },
    { "id": 9, "tasks": ["9.1", "9.2", "9.3", "9.4"] }
  ]
}
```
