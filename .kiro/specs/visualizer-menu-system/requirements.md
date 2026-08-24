# Requirements Document

## Introduction

A visually impressive menu system for the HelloDJ Discord Activity that lets viewers browse visualizer engines, navigate presets, and adjust engine-specific settings. The menu UI runs client-side in the Activity iframe and communicates with the bot backend via WebSocket for real-time state synchronization. The design follows the dark glassmorphism aesthetic with OKLCH colors, glass panels, micro-interactions, and smooth transitions befitting a music visualizer context.

## Glossary

- **Menu_System**: The client-side UI overlay in the Activity frontend that provides navigation for visualizer engines, presets, and settings.
- **Engine_Selector**: The top-level panel within the Menu_System that displays all available visualizer engines for selection.
- **Preset_Browser**: The sub-panel within the Menu_System that lists presets filtered by the currently selected engine.
- **Settings_Panel**: The sub-panel within the Menu_System that displays adjustable parameters for the active engine.
- **Activity_Frontend**: The HTML/JS/CSS application running inside Discord's Activity iframe.
- **WS_Hub**: The WebSocket hub on the bot backend that broadcasts state changes to all connected Activity viewers.
- **Guild_Settings**: The per-guild persistent configuration store on the bot backend.
- **Factory_Preset**: An immutable, always-available preset shipped with the bot (defined in factory_presets.py).
- **User_Preset**: A guild-specific preset saved by viewers that can shadow factory preset names but cannot delete them.
- **Engine_Config**: The set of adjustable parameters for a specific visualizer engine (e.g., sensitivity, blend_duration, brightness).
- **Glass_Panel**: A UI surface styled with backdrop-filter blur, semi-transparent backgrounds, subtle borders, and glow effects per the dark glassmorphism design system.

## Requirements

### Requirement 1: Menu Toggle Button

**User Story:** As a viewer, I want a clearly visible button to open and close the visualizer menu, so that I can access settings without the menu permanently obscuring the visualizer.

#### Acceptance Criteria

1. THE Activity_Frontend SHALL display a menu toggle button in the bottom-right controls area when a visualizer engine is active.
2. WHEN the toggle button is clicked, THE Menu_System SHALL open with a slide-in animation completing within 300ms.
3. WHEN the toggle button is clicked while the Menu_System is open, THE Menu_System SHALL close with a slide-out animation completing within 200ms.
4. WHILE the Menu_System is open, THE toggle button SHALL display a visually distinct active state with a brand-color glow.
5. THE toggle button SHALL have a minimum touch target of 44×44 CSS pixels.

### Requirement 2: Engine Selector

**User Story:** As a viewer, I want to browse all available visualizer engines in a visually appealing grid, so that I can switch between different visualization styles.

#### Acceptance Criteria

1. WHEN the Menu_System is opened, THE Engine_Selector SHALL display all available engines (projectm, audiovis, fosfora, varda, drift, dvd) as a grid of Glass_Panel cards.
2. THE Engine_Selector SHALL display each engine card with the engine name, a representative icon or thumbnail, and a one-line description of no more than 60 characters.
3. THE Engine_Selector SHALL visually highlight the currently active engine with a brand-color border glow and an "Active" badge.
4. WHEN a viewer selects an engine card that is not the currently active engine, THE Menu_System SHALL send an engine switch request to the WS_Hub.
5. WHEN the WS_Hub confirms the engine switch, THE Engine_Selector SHALL update the active highlight to reflect the new engine within 100ms of receiving the confirmation.
6. WHILE an engine switch is in progress, THE Engine_Selector SHALL display a loading indicator on the selected card for no longer than 10 seconds.
7. IF the WS_Hub returns an error or the engine switch does not confirm within 10 seconds, THEN THE Engine_Selector SHALL remove the loading indicator, restore the previously active engine highlight, and display an error indicator on the selected card.

### Requirement 3: Preset Browser

**User Story:** As a viewer, I want to browse and apply presets organized by engine, so that I can quickly change the visual style without manually adjusting individual settings.

#### Acceptance Criteria

1. WHEN the viewer navigates to the Preset_Browser, THE Menu_System SHALL display presets for the currently active engine grouped into Factory_Preset and User_Preset sections.
2. THE Preset_Browser SHALL display each preset as a Glass_Panel card showing the preset name and up to 4 configuration parameter values as metadata tags.
3. WHEN a viewer clicks a preset card, THE Menu_System SHALL send the preset configuration to the WS_Hub and display a loading indicator on the selected card until the WS_Hub responds.
4. WHEN the WS_Hub confirms the preset is applied, THE Preset_Browser SHALL highlight the active preset with a brand-color accent.
5. IF the active engine has no presets available, THEN THE Preset_Browser SHALL display a message indicating no presets are available for the current engine.
6. IF the preset list exceeds the visible area, THEN THE Preset_Browser SHALL allow scrolling through presets with momentum scrolling.
7. IF the WS_Hub rejects the preset apply request, THEN THE Preset_Browser SHALL remove the loading indicator from the selected card and display a brief error indicator informing the viewer the preset could not be applied.

### Requirement 4: Settings Panel

**User Story:** As a viewer, I want to adjust engine-specific settings with intuitive controls, so that I can fine-tune the visualizer to my preference.

#### Acceptance Criteria

1. WHEN the viewer navigates to the Settings_Panel, THE Menu_System SHALL display all adjustable parameters for the active engine using appropriate input controls (sliders for numeric ranges, toggles for booleans, dropdowns for enumerations).
2. THE Settings_Panel SHALL label each control with the setting name and display the current value.
3. WHEN a viewer adjusts a setting, THE Settings_Panel SHALL debounce continuous inputs (such as slider drags) and send the updated value to the WS_Hub within 100ms of the input settling.
4. WHEN the WS_Hub confirms the setting change, THE Settings_Panel SHALL reflect the confirmed value within 100ms of receiving the confirmation.
5. IF a setting value is rejected by the backend, THEN THE Settings_Panel SHALL revert the control to the previous valid value and display an error indicator for 3 seconds.
6. IF the active engine has more than 4 adjustable parameters, THEN THE Settings_Panel SHALL group settings under labeled sections as defined by the engine's settings schema grouping.

### Requirement 5: Real-Time State Synchronization

**User Story:** As a viewer joining an active session, I want the menu to reflect the current visualizer state immediately, so that I see accurate information without needing to refresh.

#### Acceptance Criteria

1. WHEN a viewer opens the Menu_System, THE Activity_Frontend SHALL request the current visualizer state from the WS_Hub.
2. WHEN the WS_Hub broadcasts a visualizer state change (engine switch, preset applied, setting changed), THE Menu_System SHALL update all affected UI elements within 200ms of receiving the message.
3. WHEN a different viewer changes the engine, preset, or setting, THE Menu_System on all connected viewers SHALL reflect the change without manual refresh.
4. WHILE the WebSocket connection is disconnected, THE Menu_System SHALL display a connection status indicator and disable interactive controls.
5. WHEN the WebSocket connection is re-established, THE Menu_System SHALL re-synchronize state from the WS_Hub.

### Requirement 6: Preset Save and Delete

**User Story:** As a viewer, I want to save the current engine configuration as a named preset and delete my saved presets, so that I can reuse preferred visualizer setups.

#### Acceptance Criteria

1. WHEN the viewer clicks a "Save Preset" button in the Settings_Panel, THE Menu_System SHALL display a Glass_Panel modal prompting for a preset name.
2. WHEN the viewer submits a valid preset name (1-50 characters, alphanumeric with hyphens and spaces), THE Menu_System SHALL send the save request to the WS_Hub.
3. WHEN the WS_Hub confirms the preset is saved, THE Preset_Browser SHALL add the new User_Preset to the list without requiring a page reload.
4. WHEN the viewer clicks a delete action on a User_Preset, THE Menu_System SHALL display a confirmation prompt before sending the delete request.
5. IF the viewer attempts to delete a Factory_Preset, THEN THE Menu_System SHALL prevent the action and display a message indicating factory presets cannot be deleted.

### Requirement 7: Visual Design and Animations

**User Story:** As a viewer, I want the menu to look visually impressive and feel responsive, so that it matches the aesthetic quality of the visualizers.

#### Acceptance Criteria

1. THE Menu_System SHALL use Glass_Panel styling with backdrop-filter blur (12px minimum), semi-transparent OKLCH-based backgrounds, and subtle animated border gradients.
2. THE Menu_System SHALL animate panel transitions (engine selector → preset browser → settings) with horizontal slide transitions completing within 250ms.
3. WHEN a viewer hovers over an interactive element, THE Menu_System SHALL display a hover state within 50ms including a subtle glow or scale micro-interaction.
4. THE Menu_System SHALL use the brand color palette (OKLCH purple spectrum: oklch(0.65 0.25 290) primary) consistently for active states, focus rings, and accent highlights.
5. WHILE the Menu_System is open, THE Menu_System SHALL render a subtle animated gradient on the panel header that shifts hue based on the active engine color identity.
6. THE Menu_System SHALL respect the prefers-reduced-motion media query by disabling animations and using instant transitions when the user has reduced motion enabled.

### Requirement 8: Responsive Layout

**User Story:** As a viewer, I want the menu to adapt to different Activity iframe sizes, so that it remains usable on both large desktop windows and compact mobile views.

#### Acceptance Criteria

1. WHEN the Activity iframe width is 600px or greater, THE Menu_System SHALL display as a side panel anchored to the right edge occupying no more than 360px width.
2. WHEN the Activity iframe width is below 600px, THE Menu_System SHALL display as a bottom sheet occupying the full width and no more than 70% of the viewport height.
3. THE Menu_System SHALL allow the viewer to drag the bottom sheet to resize its height between 30% and 70% of the viewport.
4. THE Menu_System content SHALL remain fully scrollable and interactive at all supported viewport sizes.

### Requirement 9: Keyboard and Accessibility

**User Story:** As a viewer using keyboard navigation or assistive technology, I want to navigate the menu system with full keyboard support and screen reader compatibility.

#### Acceptance Criteria

1. THE Menu_System SHALL trap focus within the menu panel while it is open and return focus to the toggle button when closed.
2. WHEN the Escape key is pressed while the Menu_System is open, THE Menu_System SHALL close.
3. THE Menu_System SHALL provide aria-label attributes on all interactive controls and aria-live regions for dynamic state changes.
4. THE Menu_System SHALL maintain a minimum color contrast ratio of 4.5:1 for text content and 3:1 for interactive UI elements against their backgrounds.
5. THE Engine_Selector cards and Preset_Browser cards SHALL be navigable via arrow keys with visible focus indicators.

### Requirement 10: Backend API for Menu Data

**User Story:** As the Activity_Frontend, I want structured API endpoints or WebSocket messages to fetch engine metadata, preset lists, and setting schemas, so that the menu can render dynamically without hardcoded data.

#### Acceptance Criteria

1. WHEN the Menu_System initializes, THE WS_Hub SHALL respond to a "menu_init" request with the list of available engines, their metadata (name, description, icon identifier), and the currently active engine and preset.
2. WHEN the viewer navigates to the Preset_Browser for a specific engine, THE WS_Hub SHALL respond to a "presets_list" request with both Factory_Preset and User_Preset entries for that engine.
3. WHEN the viewer navigates to the Settings_Panel, THE WS_Hub SHALL respond to a "settings_schema" request with the engine's configurable parameters including type, min/max bounds, default value, and current value.
4. WHEN the viewer sends an "engine_switch", "preset_apply", "setting_change", "preset_save", or "preset_delete" message, THE WS_Hub SHALL validate the request and broadcast the resulting state change to all connected viewers in the guild.
5. IF the WS_Hub receives an invalid request (unknown engine, out-of-range value, invalid preset name), THEN THE WS_Hub SHALL respond with an error message containing a human-readable description.
