#version 330 core

// Fosfora particle render — fragment shader.
// Renders soft point sprites with additive blending.

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

    // Smooth circle falloff
    float alpha = 1.0 - smoothstep(0.3, 0.5, dist);

    if (alpha < 0.01) {
        discard;
    }

    // Additive blending — output premultiplied alpha
    vec3 color = v_color.rgb * v_color.a * alpha;
    frag_color = vec4(color, alpha * v_color.a);
}
