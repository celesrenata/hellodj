#version 330 core

// Fosfora particle render — fragment shader.
// Renders soft glowing point sprites with additive blending.

in vec4 v_color;
in float v_lifetime;

out vec4 frag_color;

void main() {
    // Discard dead particles
    if (v_lifetime <= 0.0) {
        discard;
    }

    // Soft circular point sprite (using gl_PointCoord)
    vec2 coord = gl_PointCoord - vec2(0.5);
    float dist = length(coord);

    // Gaussian-like falloff for a soft glow effect
    float alpha = exp(-dist * dist * 8.0);

    if (alpha < 0.01) {
        discard;
    }

    // Bright core with soft halo — additive blending friendly
    float core = exp(-dist * dist * 32.0);  // tight bright center
    vec3 color = v_color.rgb * (0.6 + core * 0.4);

    // Output premultiplied alpha for additive blending
    frag_color = vec4(color * alpha * v_color.a, alpha * v_color.a);
}
