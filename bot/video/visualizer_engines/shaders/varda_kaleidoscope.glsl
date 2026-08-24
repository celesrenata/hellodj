#version 330 core

// Varda: Audio-reactive kaleidoscope with fractal geometry.
// Mirrors and folds space with beat-driven rotation.

uniform float     iTime;
uniform vec2      iResolution;
uniform sampler2D iChannel0;
uniform float     iBeat;
uniform float     iBPM;
uniform float     iBandEnergy[7];

in vec2 fragCoord;
out vec4 fragColor;

#define PI 3.14159265359

// Fold space around an axis at given angle
vec2 fold(vec2 p, float angle) {
    vec2 n = vec2(cos(angle), sin(angle));
    float d = dot(p, n);
    if (d < 0.0) p -= 2.0 * d * n;
    return p;
}

void main() {
    vec2 uv = fragCoord * 2.0 - 1.0;
    uv.x *= iResolution.x / iResolution.y;

    float t = iTime * 0.4;
    float bass = iBandEnergy[0] + iBandEnergy[1];
    float mids = iBandEnergy[2] + iBandEnergy[3] + iBandEnergy[4];
    float highs = iBandEnergy[5] + iBandEnergy[6];

    // Rotate with time and beat
    float rot = t + iBeat * 0.5;
    float c = cos(rot), s = sin(rot);
    uv = mat2(c, -s, s, c) * uv;

    // Kaleidoscope: fold space N times
    int folds = 6 + int(bass * 2.0);
    float foldAngle = PI / float(folds);
    for (int i = 0; i < 8; i++) {
        if (i >= folds) break;
        uv = fold(uv, foldAngle * float(i) + t * 0.1);
    }

    // Zoom pulsing with beat
    float zoom = 1.5 + iBeat * 0.5 + bass * 0.3;
    uv *= zoom;

    // Fractal pattern in folded space
    vec3 col = vec3(0.0);
    float intensity = 0.0;
    vec2 z = uv;

    for (int i = 0; i < 5; i++) {
        // Twist and scale
        z = abs(z) - 0.8;
        float a = t * 0.3 + float(i) * 0.7;
        float ca = cos(a), sa = sin(a);
        z = mat2(ca, -sa, sa, ca) * z;
        z *= 1.2 + mids * 0.3;

        // Accumulate glow based on distance from pattern edges
        float d = length(z) - 0.5;
        intensity += exp(-abs(d) * (3.0 + highs * 5.0));
    }

    intensity *= 0.2;

    // Multi-hue coloring based on iteration depth and time
    float hue1 = t * 0.2 + bass * 0.5;
    float hue2 = t * 0.15 + 0.33;
    float hue3 = t * 0.1 + 0.66;

    col.r = intensity * (0.5 + 0.5 * sin(hue1 * 6.28));
    col.g = intensity * (0.5 + 0.5 * sin(hue2 * 6.28));
    col.b = intensity * (0.5 + 0.5 * sin(hue3 * 6.28));

    // Boost on beat
    col *= 1.0 + iBeat * 1.0;

    // Add warm glow at center
    float centerDist = length(fragCoord - 0.5);
    col += vec3(0.8, 0.3, 0.1) * exp(-centerDist * 4.0) * (0.2 + bass * 0.5);

    // Tone map to avoid blowout
    col = col / (1.0 + col);

    fragColor = vec4(col, 1.0);
}
