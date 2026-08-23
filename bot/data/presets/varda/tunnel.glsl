#version 330 core

// Beat-reactive neon tunnel fly-through.
// Shadertoy-compatible uniform convention.

uniform float     iTime;
uniform vec2      iResolution;
uniform sampler2D iChannel0;    // 512x2 audio texture (row 0: waveform, row 1: FFT)
uniform float     iBeat;        // 0-1 decaying beat pulse
uniform float     iBPM;
uniform float     iBandEnergy[7];

in vec2 fragCoord;
out vec4 fragColor;

void main() {
    vec2 uv = fragCoord;
    vec2 p = uv * 2.0 - 1.0;
    p.x *= iResolution.x / iResolution.y;

    float bass = iBandEnergy[0] + iBandEnergy[1];
    float mid = iBandEnergy[2] + iBandEnergy[3];

    // Polar coordinates for tunnel
    float angle = atan(p.y, p.x);
    float radius = length(p);

    // Tunnel mapping (inverse radius for depth)
    float depth = 1.0 / (radius + 0.001);
    float speed = iTime * (1.5 + bass * 2.0);

    // Texture coordinates
    float tx = depth + speed;
    float ty = angle / 3.14159;

    // Neon ring pattern
    float rings = sin(tx * 8.0) * 0.5 + 0.5;
    float segments = sin(ty * 12.0 + iTime) * 0.5 + 0.5;
    float pattern = rings * 0.7 + segments * 0.3;

    // Beat-reactive glow
    float glow = 1.0 / (radius * 3.0 + 0.5);
    glow *= 1.0 + iBeat * 2.0;

    // Neon colors driven by mid frequencies
    vec3 col1 = vec3(0.1, 0.5, 1.0);  // blue
    vec3 col2 = vec3(1.0, 0.2, 0.8);  // magenta
    vec3 color = mix(col1, col2, sin(angle + iTime) * 0.5 + 0.5);

    color *= pattern * glow;
    color += vec3(0.05, 0.1, 0.2) * mid;

    // Vignette
    float vignette = 1.0 - radius * 0.3;
    color *= max(vignette, 0.0);

    fragColor = vec4(color, 1.0);
}
