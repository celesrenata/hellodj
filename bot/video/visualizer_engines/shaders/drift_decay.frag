#version 330 core

// Drift: Decay multiplication pass.
// Full-screen quad shader that multiplies the current frame by a decay
// factor, preventing infinite brightness accumulation in the feedback loop.
//
// The decay factor is modulated by audio energy:
//   - More bass → faster fade (shorter trails)
//   - Less bass → slower fade (longer, more persistent trails)
//
// This runs as a separate pass after the warp pass, allowing independent
// control of trail persistence vs. warp motion.

out vec4 frag_color;

in vec2 v_uv;

uniform sampler2D u_prev_frame;  // Current frame (output of warp pass)
uniform float u_decay_base;      // Base decay factor (0.94-0.99, preset-driven)
uniform float u_bass;            // Low frequency energy
uniform float u_mids;            // Mid frequency energy
uniform float u_highs;           // High frequency energy
uniform float u_energy;          // Overall audio energy (0-1)
uniform float u_time;            // Elapsed time (seconds)

void main() {
    vec2 uv = v_uv;

    // Sample the current frame (post-warp)
    vec4 color = texture(u_prev_frame, uv);

    // Compute audio-modulated decay:
    // More bass → faster fade (decay decreases)
    // High energy → slightly faster fade to prevent white-out
    float decay = u_decay_base - u_bass * 0.02 - u_energy * 0.005;

    // Clamp to sane range: never fully opaque (would freeze), never too fast
    decay = clamp(decay, 0.90, 0.995);

    // Apply decay multiplication
    frag_color = color * decay;
}
