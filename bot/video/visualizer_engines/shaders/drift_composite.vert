#version 330 core

// Drift: Composite pass vertex shader.
// Fullscreen triangle (no VBO needed, uses gl_VertexID).

out vec2 v_uv;

void main() {
    float x = float((gl_VertexID & 1) << 2) - 1.0;
    float y = float((gl_VertexID & 2) << 1) - 1.0;
    gl_Position = vec4(x, y, 0.0, 1.0);
    v_uv = vec2((x + 1.0) * 0.5, (y + 1.0) * 0.5);
}
