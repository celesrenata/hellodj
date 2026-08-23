#version 330 core

// Default fallback shader — classic plasma effect with audio-driven color cycling.
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

    float t = iTime * 0.5;

    // Sample bass energy for color intensity
    float bass = iBandEnergy[0] + iBandEnergy[1];

    // Classic plasma formula with audio modulation
    float v1 = sin(p.x * 3.0 + t);
    float v2 = sin(p.y * 3.0 + t * 0.7);
    float v3 = sin((p.x + p.y) * 2.0 + t * 1.3);
    float v4 = sin(length(p) * 4.0 - t * 2.0);

    float plasma = (v1 + v2 + v3 + v4) * 0.25;

    // Audio-reactive color cycling
    float hueShift = bass * 0.5 + iBeat * 0.3;
    float r = sin(plasma * 3.14159 + hueShift) * 0.5 + 0.5;
    float g = sin(plasma * 3.14159 + hueShift + 2.094) * 0.5 + 0.5;
    float b = sin(plasma * 3.14159 + hueShift + 4.189) * 0.5 + 0.5;

    // Beat-reactive brightness boost
    float brightness = 1.0 + iBeat * 0.5;

    fragColor = vec4(vec3(r, g, b) * brightness, 1.0);
}
