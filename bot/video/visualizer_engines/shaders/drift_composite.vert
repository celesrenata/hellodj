#version 330 core

// Drift: Composite pass vertex shader.
// Fullscreen triangle using layout(location=0) in vec2 aPos for Mesa iris
// compatibility (gl_VertexID-only tricks fail on Mesa iris driver).
// Expects a 3-vertex VBO covering [-1,1] clip space.

layout(location = 0) in vec2 aPos;

out vec2 v_uv;

void main() {
    gl_Position = vec4(aPos, 0.0, 1.0);
    v_uv = aPos * 0.5 + 0.5;
}
