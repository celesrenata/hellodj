#version 330 core

// Drift: Final compositing pass.
// Combines the main feedback buffer with the bloom buffer.

in vec2 v_uv;
out vec4 frag_color;

uniform sampler2D u_main;    // Main feedback FBO (full res)
uniform sampler2D u_bloom;   // Blurred bloom FBO (half res, upsampled by texture filtering)
uniform float u_bloom_intensity;  // 0.0 - 1.0

void main() {
    vec3 main_color = texture(u_main, v_uv).rgb;
    vec3 bloom_color = texture(u_bloom, v_uv).rgb;

    // Additive bloom
    vec3 result = main_color + bloom_color * u_bloom_intensity;

    // Subtle tone mapping to prevent blowout
    result = result / (1.0 + result * 0.3);

    // Minimal vignette (just darkens corners slightly)
    float vig = 1.0 - length(v_uv - 0.5) * 0.3;
    result *= vig;

    frag_color = vec4(result, 1.0);
}
