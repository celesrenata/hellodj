#version 330 core

// Drift: Vertical Gaussian blur pass (separable bloom).

in vec2 v_uv;
out vec4 frag_color;

uniform sampler2D u_source;
uniform float u_texel_size;  // 1.0 / texture_height

const float weights[5] = float[](0.2270270, 0.1945946, 0.1216216, 0.0540540, 0.0162162);

void main() {
    vec3 result = texture(u_source, v_uv).rgb * weights[0];

    for (int i = 1; i < 5; i++) {
        float offset = float(i) * u_texel_size;
        result += texture(u_source, v_uv + vec2(0.0, offset)).rgb * weights[i];
        result += texture(u_source, v_uv - vec2(0.0, offset)).rgb * weights[i];
    }

    frag_color = vec4(result, 1.0);
}
