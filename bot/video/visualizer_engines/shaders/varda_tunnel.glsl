#version 330 core

// Varda: Infinite tunnel with fractal distortion and audio-reactive warping.
// Deep Z-motion with pulsing geometry.

uniform float     iTime;
uniform vec2      iResolution;
uniform sampler2D iChannel0;
uniform float     iBeat;
uniform float     iBPM;
uniform float     iBandEnergy[7];

in vec2 fragCoord;
out vec4 fragColor;

void main() {
    vec2 uv = fragCoord * 2.0 - 1.0;
    uv.x *= iResolution.x / iResolution.y;

    float t = iTime * 0.8;
    float bass = iBandEnergy[0] + iBandEnergy[1];
    float mids = iBandEnergy[2] + iBandEnergy[3] + iBandEnergy[4];
    float highs = iBandEnergy[5] + iBandEnergy[6];

    // Tunnel polar coordinates
    float angle = atan(uv.y, uv.x);
    float radius = length(uv);

    // Warp radius with beat
    radius += iBeat * 0.1 * sin(angle * 3.0 + t * 2.0);

    // Tunnel depth (1/r mapping)
    float depth = 0.5 / (radius + 0.01);

    // Scroll through tunnel
    float z = depth + t * 1.5;

    // Texture coordinates in tunnel space
    float tx = angle / 3.14159 + sin(z * 0.3) * 0.1 * bass;
    float ty = z;

    // Fractal-like repetition pattern
    float pattern = 0.0;
    float scale = 1.0;
    for (int i = 0; i < 4; i++) {
        vec2 p = vec2(tx * scale, ty * scale);
        float cell = abs(sin(p.x * 6.28) * sin(p.y * 6.28));
        cell = pow(cell, 2.0 + mids * 3.0);
        pattern += cell / scale;
        scale *= 2.1;
    }

    // Edge glow (brighter near tunnel walls)
    float edgeGlow = pow(radius, 3.0) * 2.0;

    // Color — deep purple/cyan palette
    vec3 col = vec3(0.0);
    col.r = pattern * 0.4 + edgeGlow * 0.6 * (0.3 + bass * 0.5);
    col.g = pattern * 0.2 + edgeGlow * 0.3 * (0.5 + highs * 0.3);
    col.b = pattern * 0.8 + edgeGlow * 1.0 * (0.6 + mids * 0.2);

    // Beat flash — bright cyan pulse from center
    float centerFlash = (1.0 - radius) * iBeat * 1.5;
    col += vec3(0.1, 0.6, 0.9) * centerFlash;

    // Depth fog (darker toward center/infinity)
    col *= smoothstep(0.0, 0.3, radius);

    // Vignette
    float vig = 1.0 - radius * 0.4;
    col *= vig;

    fragColor = vec4(col, 1.0);
}
