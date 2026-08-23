#version 330 core

// Fosfora particle render — vertex shader.
// Positions particles as point sprites with size based on lifetime.

layout(location = 0) in vec3 in_position;
layout(location = 1) in vec3 in_velocity;
layout(location = 2) in float in_lifetime;
layout(location = 3) in vec4 in_color;

out vec4 v_color;
out float v_lifetime;

uniform mat4 u_projection;
uniform float u_point_size_base;
uniform float u_trail_length;

void main() {
    // Discard dead particles by moving offscreen
    if (in_lifetime <= 0.0) {
        gl_Position = vec4(2.0, 2.0, 2.0, 1.0);
        gl_PointSize = 0.0;
        v_color = vec4(0.0);
        v_lifetime = 0.0;
        return;
    }

    gl_Position = u_projection * vec4(in_position, 1.0);

    // Point size scales with lifetime (bigger when fresh, shrinks as it dies)
    float size_factor = smoothstep(0.0, 1.0, in_lifetime / 3.0);
    gl_PointSize = u_point_size_base * (0.5 + size_factor * 1.5);

    v_color = in_color;
    v_lifetime = in_lifetime;
}
