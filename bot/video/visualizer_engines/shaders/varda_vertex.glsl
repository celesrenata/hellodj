#version 330 core

// Fullscreen triangle vertex shader for Varda engine.
// Generates a single triangle that covers the entire viewport without
// needing any vertex buffer (uses gl_VertexID).

out vec2 fragCoord;

void main() {
    // Generate positions for a fullscreen triangle:
    //   vertex 0: (-1, -1)
    //   vertex 1: ( 3, -1)
    //   vertex 2: (-1,  3)
    // This covers the entire [-1,1] clip space with one triangle.
    float x = float((gl_VertexID & 1) << 2) - 1.0;
    float y = float((gl_VertexID & 2) << 1) - 1.0;
    gl_Position = vec4(x, y, 0.0, 1.0);
    // Map to [0, resolution] in fragment shader via iResolution
    fragCoord = vec2((x + 1.0) * 0.5, (y + 1.0) * 0.5);
}
