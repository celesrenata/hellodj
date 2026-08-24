#version 330 core

// Drift: Warp pass fragment shader.
// Samples the previous frame at the warped UV, applies decay.

in vec2 v_uv;
out vec4 frag_color;

uniform sampler2D u_prev_frame;
uniform float u_decay;       // Frame persistence (0.94-0.99)
uniform float u_bass;

void main() {
    // Clamp UVs to prevent sampling outside texture (creates hard edges)
    vec2 uv = clamp(v_uv, 0.001, 0.999);

    // Sample previous frame
    vec4 prev = texture(u_prev_frame, uv);

    // Apply decay (modulated by bass — more bass = faster fade)
    float decay = u_decay - u_bass * 0.015;
    decay = clamp(decay, 0.90, 0.995);

    frag_color = prev * decay;
}
