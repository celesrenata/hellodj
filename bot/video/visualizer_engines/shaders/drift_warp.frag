#version 330 core

// Drift: Warp pass fragment shader.
// Samples the previous frame texture at the warped UV coordinates
// provided by the vertex shader. This is the core of the feedback loop:
// the warp mesh displaces UVs per-vertex, and this shader performs the
// texture lookup to create the flowing/trailing motion.
//
// No decay is applied here — that is handled by a separate decay pass
// (drift_decay.frag) which runs as a full-screen quad afterwards.

in vec2 v_uv;
out vec4 frag_color;

uniform sampler2D u_prev_frame;

void main() {
    // Clamp UVs to prevent sampling outside texture bounds.
    // Using a small epsilon avoids border artifacts on some drivers.
    vec2 uv = clamp(v_uv, 0.001, 0.999);

    // Sample previous frame at the warped UV coordinate
    frag_color = texture(u_prev_frame, uv);
}
