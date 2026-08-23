#version 330 core

// Infinite Mandelbrot zoom driven by time and bass energy.
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

    // Zoom speed driven by bass energy
    float zoom = exp(-iTime * (0.4 + bass * 0.3));
    vec2 c = vec2(-0.745, 0.186) + p * zoom;

    // Mandelbrot iteration
    vec2 z = vec2(0.0);
    float iter = 0.0;
    const float maxIter = 128.0;

    for (float i = 0.0; i < maxIter; i++) {
        z = vec2(z.x * z.x - z.y * z.y, 2.0 * z.x * z.y) + c;
        if (dot(z, z) > 4.0) break;
        iter = i;
    }

    // Smooth coloring
    float smooth_iter = iter - log2(log2(dot(z, z)));
    float t = smooth_iter / maxIter;

    // Audio-reactive palette with beat pulse
    float hue = t * 6.28 + iTime * 0.3 + iBeat * 1.5;
    vec3 col = 0.5 + 0.5 * cos(hue + vec3(0.0, 2.094, 4.189));
    col *= 1.0 + iBeat * 0.6;

    // Darken the set interior
    if (iter >= maxIter - 1.0) col = vec3(0.0);

    fragColor = vec4(col, 1.0);
}
