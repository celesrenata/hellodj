#version 330 core

// Fullscreen quad vertex shader for AudioVis engine.
// Emits a fullscreen triangle-strip quad covering [-1,1] in NDC.

out vec2 vUV;

void main() {
    // Generate fullscreen quad from vertex ID (0..3)
    // Triangle strip: 0=BL, 1=BR, 2=TL, 3=TR
    vec2 positions[4] = vec2[](
        vec2(-1.0, -1.0),
        vec2( 1.0, -1.0),
        vec2(-1.0,  1.0),
        vec2( 1.0,  1.0)
    );

    vec2 pos = positions[gl_VertexID];
    vUV = pos * 0.5 + 0.5;  // [0,1] UV coordinates
    gl_Position = vec4(pos, 0.0, 1.0);
}
