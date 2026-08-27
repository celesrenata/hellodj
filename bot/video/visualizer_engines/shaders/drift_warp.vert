#version 330 core

// Drift: Warp mesh vertex shader.
// Displaces a 48×36 grid mesh's UVs to create per-vertex motion on the
// feedback texture. Each vertex samples the previous frame at a warped UV
// coordinate, producing organic flowing trails (Milkdrop-style feedback loop).
//
// Audio-driven transformations per vertex:
//   1. Zoom (bass-driven) — expand from center
//   2. Rotation (mids-driven) — rotate UVs around center
//   3. Per-vertex displacement — organic flow patterns from sin/cos fields

layout(location = 0) in vec2 in_position;  // Grid vertex position [-1, 1]
layout(location = 1) in vec2 in_uv;        // Base UV [0, 1]

out vec2 v_uv;  // Warped UV for sampling previous frame

// Audio uniforms
uniform float u_time;
uniform float u_bass;       // Low frequency energy (bands 0-1)
uniform float u_mids;       // Mid frequency energy (bands 2-4)
uniform float u_highs;      // High frequency energy (bands 5-6)
uniform float u_energy;     // Overall audio energy (0-1)
uniform float u_beat;       // 0-1 beat pulse (decaying)

// Warp parameters (from preset, set by DriftEngine)
uniform float u_zoom_base;       // Base zoom per frame (e.g. 1.005 = slow zoom in)
uniform float u_zoom_bass;       // Additional zoom on bass energy
uniform float u_zoom_beat;       // Zoom pulse on beat transient
uniform float u_rot_base;        // Base rotation per frame (radians)
uniform float u_rot_mids;        // Rotation driven by mids energy
uniform float u_warp_x_freq;     // X displacement frequency multiplier
uniform float u_warp_y_freq;     // Y displacement frequency multiplier
uniform float u_warp_amplitude;  // Displacement strength

void main() {
    gl_Position = vec4(in_position, 0.0, 1.0);

    // Start with base UV
    vec2 uv = in_uv;

    // --- Zoom (center zoom driven by bass) ---
    // More bass → more zoom → trails rush toward edges
    float zoom = u_zoom_base + u_bass * u_zoom_bass + u_beat * u_zoom_beat;
    uv = (uv - 0.5) / zoom + 0.5;

    // --- Rotation (driven by mid energy) ---
    // Creates swirling motion in the feedback loop
    float angle = u_rot_base + u_mids * u_rot_mids;
    vec2 centered = uv - 0.5;
    float ca = cos(angle);
    float sa = sin(angle);
    uv = vec2(
        centered.x * ca - centered.y * sa,
        centered.x * sa + centered.y * ca
    ) + 0.5;

    // --- Per-vertex displacement (creates organic flow patterns) ---
    // Sine/cosine field modulated by audio for non-uniform warping
    float dx = sin(in_uv.y * 6.2832 * u_warp_y_freq + u_time * 1.5) * u_warp_amplitude;
    float dy = cos(in_uv.x * 6.2832 * u_warp_x_freq + u_time * 1.2) * u_warp_amplitude;

    // Scale displacement by audio energy for responsiveness
    dx *= (1.0 + u_bass * 2.0 + u_beat + u_energy * 0.5);
    dy *= (1.0 + u_highs * 1.5 + u_beat + u_energy * 0.5);

    uv.x += dx;
    uv.y += dy;

    v_uv = uv;
}
