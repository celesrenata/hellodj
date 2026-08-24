#version 330 core

// Fullscreen quad vertex shader for AudioVis engine.
// Reads position from vertex attribute 0 (bound VBO).

layout(location = 0) in vec2 aPos;
out vec2 vUV;

void main() {
    vUV = aPos * 0.5 + 0.5;  // [-1,1] → [0,1] UV coordinates
    gl_Position = vec4(aPos, 0.0, 1.0);
}
